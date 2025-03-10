# -*- coding: utf-8 -*-
import zipfile
import tempfile
from django.http import HttpResponse
from django.utils.encoding import smart_str
from django.contrib.gis.db.models.fields import GeometryField
from django.contrib.gis.gdal import check_err, OGRGeomType
# python logging support to django logging middleware
import logging
logger = logging.getLogger(__name__)

try:
    from io import BytesIO
except ImportError:
    from io import BytesIO

try:
    # a mysterious bug with ctypes and python26 causes crashes
    # when calling lgdal.OGR_DS_CreateLayer, so we default to
    # using the native python bindings to ogr/gdal if they exist
    # thanks Jared K, for reporting this bug and submitting an alternative
    # approach
    from osgeo import gdal, ogr, osr
    HAS_NATIVE_BINDINGS = True
except ImportError:
    HAS_NATIVE_BINDINGS = False
    from django.contrib.gis.gdal.libgdal import lgdal
    from django.contrib.gis.gdal import (
        Driver, OGRGeometry, OGRGeomType, SpatialReference, check_err,
        CoordTransform
    )


class ShpResponder(object):
    def __init__(
            self, queryset, readme=None, geo_field=None, proj_transform=None,
            content_type='application/zip', file_name='shp_download'):
        self.queryset = queryset
        self.readme = readme
        self.geo_field = geo_field
        self.proj_transform = proj_transform
        self.content_type = content_type
        self.file_name = smart_str(file_name)

    def __call__(self, *args, **kwargs):
        """
        Method that gets called when the ShpResponder class is used as a view.

        Example
        -------

        from shapes import ShpResponder

        shp_response = ShpResponder(
            Model.objects.all(),proj_transform=900913,file_name=u"fo\xf6.txt")

        urlpatterns = patterns('',
            (r'^export_shp/$', shp_response),
            )

        """

        tmp = self.write_shapefile_to_tmp_file(self.queryset)
        return self.zip_response(
            tmp, self.file_name, self.content_type, self.readme)

    def get_attributes(self):
        # Todo: control field order as param
        fields = self.queryset.model._meta.fields
        attr = [f for f in fields if not isinstance(f, GeometryField)]
        return attr

    def get_geo_field(self):
        fields = self.queryset.model._meta.fields
        geo_fields = [f for f in fields if isinstance(f, GeometryField)]
        geo_fields_names = ', '.join([f.name for f in geo_fields])

        if len(geo_fields) > 1:
            if not self.geo_field:
                raise ValueError(
                    "More than one geodjango geometry field found, please "
                    "specify which to use by name using the 'geo_field' "
                    "keyword. Available fields are: '%s'" % geo_fields_names)
            else:
                geo_field_by_name = [
                    fld for fld in geo_fields if fld.name == self.geo_field]
                if not geo_field_by_name:
                    raise ValueError(
                        "Geodjango geometry field not found with the name '%s'"
                        ", fields available are: '%s'" % (
                            self.geo_field, geo_fields_names))
                else:
                    geo_field = geo_field_by_name[0]
        elif geo_fields:
            geo_field = geo_fields[0]
        else:
            raise ValueError(
                'No geodjango geometry fields found in this model queryset')

        return geo_field

    def write_shapefile_to_tmp_file(self, queryset):
        tmp = tempfile.NamedTemporaryFile(suffix='.shp', mode='w+b')
        # we must close the file for GDAL to be able to open and write to it
        tmp.close()
        args = tmp.name, queryset, self.get_geo_field()

        if HAS_NATIVE_BINDINGS:
            self.write_with_native_bindings(*args)
        else:
            self.write_with_ctypes_bindings(*args)

        return tmp.name

    def zip_response(self, shapefile_path, file_name, content_type, readme=None):
        buffer = BytesIO()
        zip = zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED)
        files = ['shp', 'shx', 'prj', 'dbf']
        for item in files:
            filename = '%s.%s' % (shapefile_path.replace('.shp', ''), item)
            zip.write(
                filename,
                arcname='%s.%s' % (file_name.replace('.shp', ''), item))
        if readme:
            zip.writestr('README.txt', readme)
        zip.close()
        buffer.flush()
        zip_stream = buffer.getvalue()
        buffer.close()

        # Stick it all in a django HttpResponse
        response = HttpResponse('', content_type)
        response['Content-Disposition'] = (
            'attachment; filename=%s.zip' % file_name.replace('.shp', '')
        )
        response['Content-length'] = str(len(zip_stream))
        response['Content-Type'] = content_type
        response.write(zip_stream)
        return response

    def write_with_native_bindings(self, tmp_name, queryset, geo_field):
        """ Write a shapefile out to a file from a geoqueryset.

        Written by Jared Kibele and Dane Springmeyer.

        In this case we use the python bindings available with a build
        of gdal when compiled with --with-python, instead of the ctypes-based
        bindings that GeoDjango provides.

        """

        dr = ogr.GetDriverByName('ESRI Shapefile')
        ds = dr.CreateDataSource(tmp_name)
        if ds is None:
            raise Exception('Could not create file!')

        if hasattr(geo_field, 'geom_type'):
            ogr_type = OGRGeomType(geo_field.geom_type).num
        else:
            ogr_type = OGRGeomType(geo_field._geom).num

        native_srs = osr.SpatialReference()
        if hasattr(geo_field, 'srid'):
            native_srs.ImportFromEPSG(geo_field.srid)
        else:
            native_srs.ImportFromEPSG(geo_field._srid)

        if self.proj_transform:
            output_srs = osr.SpatialReference()
            output_srs.ImportFromEPSG(self.proj_transform)
        else:
            output_srs = native_srs

        layer = ds.CreateLayer('lyr', srs=output_srs, geom_type=ogr_type)

        attributes = self.get_attributes()

        for field in attributes:
            field_defn = ogr.FieldDefn(str(field.name), ogr.OFTString)
            field_defn.SetWidth(255)
            if layer.CreateField(field_defn) != 0:
                raise Exception('Faild to create field')

        feature_def = layer.GetLayerDefn()

        for item in queryset:
            feat = ogr.Feature(feature_def)

            for field in attributes:
                value = getattr(item, field.name)
                try:
                    string_value = str(value)
                except UnicodeEncodeError as E:
                    string_value = ''
                feat.SetField(str(field.name), string_value)

            geom = getattr(item, geo_field.name)

            if geom:
                ogr_geom = ogr.CreateGeometryFromWkt(geom.wkt)
                if self.proj_transform:
                    ct = osr.CoordinateTransformation(native_srs, output_srs)
                    ogr_geom.Transform(ct)
                check_err(feat.SetGeometry(ogr_geom))
            else:
                pass

            check_err(layer.CreateFeature(feat))

        ds.Destroy()

    def write_with_ctypes_bindings(self, tmp_name, queryset, geo_field):
        """ Write a shapefile out to a file from a geoqueryset.

        Uses GeoDjangos ctypes wrapper to ogr in libgdal.

        """

        # Get the shapefile driver
        dr = Driver('ESRI Shapefile')

        # Creating the datasource
        ds = lgdal.OGR_Dr_CreateDataSource(dr._ptr, tmp_name, None)
        if ds is None:
            raise Exception('Could not create file!')

        # Get the right geometry type number for ogr
        if hasattr(geo_field, 'geom_type'):
            ogr_type = OGRGeomType(geo_field.geom_type).num
        else:
            ogr_type = OGRGeomType(geo_field._geom).num

        # Set up the native spatial reference of the geometry field using the
        # srid
        if hasattr(geo_field, 'srid'):
            native_srs = SpatialReference(geo_field.srid)
        else:
            native_srs = SpatialReference(geo_field._srid)

        if self.proj_transform:
            output_srs = SpatialReference(self.proj_transform)
        else:
            output_srs = native_srs

        # create the layer
        # this is crashing python26 on osx and ubuntu
        layer = lgdal.OGR_DS_CreateLayer(
            ds, 'lyr', output_srs._ptr, ogr_type, None)

        # Create the fields
        attributes = self.get_attributes()

        for field in attributes:
            fld = lgdal.OGR_Fld_Create(str(field.name), 4)
            added = lgdal.OGR_L_CreateField(layer, fld, 0)
            check_err(added)

        # Getting the Layer feature definition.
        feature_def = lgdal.OGR_L_GetLayerDefn(layer)

        # Loop through queryset creating features
        for item in self.queryset:
            feat = lgdal.OGR_F_Create(feature_def)

            # For now, set all fields as strings
            # TODO: catch model types and convert to ogr fields
            # http://www.gdal.org/ogr/classOGRFeature.html

            # OGR_F_SetFieldDouble
            #OFTReal => FloatField DecimalField

            # OGR_F_SetFieldInteger
            #OFTInteger => IntegerField

            #OGR_F_SetFieldStrin
            #OFTString => CharField

            # OGR_F_SetFieldDateTime()
            #OFTDateTime => DateTimeField
            #OFTDate => TimeField
            #OFTDate => DateField

            for idx, field in enumerate(attributes):
                value = getattr(item, field.name)
                try:
                    string_value = str(value)
                except UnicodeEncodeError as E:
                    # pass for now....
                    # http://trac.osgeo.org/gdal/ticket/882
                    string_value = ''
                lgdal.OGR_F_SetFieldString(feat, idx, string_value)

            # Transforming & setting the geometry
            geom = getattr(item, geo_field.name)

            # if requested we transform the input geometry
            # to match the shapefiles projection 'to-be'
            if geom:
                ogr_geom = OGRGeometry(geom.wkt, output_srs)
                if self.proj_transform:
                    ct = CoordTransform(native_srs, output_srs)
                    ogr_geom.transform(ct)
                # create the geometry
                check_err(lgdal.OGR_F_SetGeometry(feat, ogr_geom._ptr))
            else:
                # Case where geometry object is not found because of null value
                # for field effectively looses whole record in shapefile if
                # geometry does not exist
                pass

            # creat the feature in the layer.
            check_err(lgdal.OGR_L_SetFeature(layer, feat))

        # Cleaning up
        check_err(lgdal.OGR_L_SyncToDisk(layer))
        lgdal.OGR_DS_Destroy(ds)
        lgdal.OGRCleanupAll()

    def write_search_records(self, recordsArray):
        """
          Write a shapefile out to a file from a geoqueryset.
          Added by Tim based on code written by Kibele and Dane Springmeyer.

        In this case we use the python bindings available with a build
        of gdal when compiled with --with-python, instead of the ctypes-based
        bindings that GeoDjango provides.

        Also in this case we dont need a model but a searcher array of search
        objects
        @return an HttpResponse with shp payload
        """
        tmp = tempfile.NamedTemporaryFile(suffix='.shp', mode='w+b')
        # we must close the file for GDAL to be able to open and write to it
        tmp.close()

        dr = ogr.GetDriverByName('ESRI Shapefile')
        ds = dr.CreateDataSource(tmp.name)
        if ds is None:
            raise Exception('Could not create file!')

        native_srs = osr.SpatialReference()
        native_srs.ImportFromEPSG(4326)  # latlong wgs84

        if self.proj_transform:
            output_srs = osr.SpatialReference()
            output_srs.ImportFromEPSG(self.proj_transform)
        else:
            output_srs = native_srs

        ogr_type = OGRGeomType('POLYGON').num
        layer = ds.CreateLayer('lyr', srs=output_srs, geom_type=ogr_type)

        attributes= []
        attributes.append("product_id")
        attributes.append("satellite")
        attributes.append("instrument_type")
        attributes.append("product_profile")
        attributes.append("processing_level")
        attributes.append("owner")
        attributes.append("license")
        attributes.append("product_acquisition_start")
        attributes.append("product_acquisition_end")
        attributes.append("projection")
        attributes.append("quality")
        attributes.append("geometric_accuracy_mean")
        attributes.append("geometric_accuracy_1sigma")
        attributes.append("geometric_accuracy_2sigma")
        attributes.append("spectral_accuracy")
        attributes.append("radiometric_signal_to_noise_ratio")
        attributes.append("radiometric_percentage_error")
        attributes.append("spatial_resolution_x")
        attributes.append("spatial_resolution_y")
        attributes.append("spectral_resolution")
        attributes.append("radiometric_resolution")
        attributes.append("creating_software")
        attributes.append("original_product_id")
        attributes.append("orbit_number")
        attributes.append("product_revision")
        attributes.append("path")
        attributes.append("path_offset")
        attributes.append("row")
        attributes.append("row_offset")

        for field in attributes:
            field_defn = ogr.FieldDefn(str(field), ogr.OFTString)
            field_defn.SetWidth(255)
            if layer.CreateField(field_defn) != 0:
                raise Exception('Faild to create field')

        feature_def = layer.GetLayerDefn()

        for item in recordsArray:
            feat = ogr.Feature(feature_def)

            for field in attributes:
                #ABP: added getConcreteProduct and None
                value = getattr(
                    item.product.getConcreteInstance(), field, None)
                logger.info("Shape writer: Setting %s to %s" % (field, value))
                try:
                    string_value = str(value)
                except UnicodeEncodeError as E:
                    string_value = ''
                    logger.info("Unicode conversion error")
                #truncate field name to 10 letters to deal with shp limitations
                logger.info("Truncated field name: %s" % str(field[0:10]))
                feat.SetField(str(field[0:10]), string_value)

            geom = getattr(item.product, "spatial_coverage")

            if geom:
                ogr_geom = ogr.CreateGeometryFromWkt(geom.wkt)
                if self.proj_transform:
                    ct = osr.CoordinateTransformation(native_srs, output_srs)
                    ogr_geom.Transform(ct)
                check_err(feat.SetGeometry(ogr_geom))
            else:
                pass

            check_err(layer.CreateFeature(feat))

        ds.Destroy()
        # Next line for debugging only if you want to see log info in
        # debugtoolbar
        #return HttpResponse("<html><head></head><body>Done</body></html>")
        content_type = 'application/zip'
        return self.zip_response(tmp.name, self.file_name, content_type)

    def write_request_records(self, recordsArray):
        """
          Write a shapefile out to a file from a tasking request.
          Added by Tim based on code written by Kibele and Dane Springmeyer.

        In this case we use the python bindings available with a build
        of gdal when compiled with --with-python, instead of the ctypes-based
        bindings that GeoDjango provides.

        Also in this case we dont need a model but a searcher array of search
        objects

        @return an HttpResponse with shp payload
        """
        tmp = tempfile.NamedTemporaryFile(suffix='.shp', mode='w+b')
        # we must close the file for GDAL to be able to open and write to it
        tmp.close()

        dr = ogr.GetDriverByName('ESRI Shapefile')
        ds = dr.CreateDataSource(tmp.name)
        if ds is None:
            raise Exception('Could not create file!')

        native_srs = osr.SpatialReference()
        native_srs.ImportFromEPSG(4326)  # latlong wgs84

        if self.proj_transform:
            output_srs = osr.SpatialReference()
            output_srs.ImportFromEPSG(self.proj_transform)
        else:
            output_srs = native_srs

        ogr_type = OGRGeomType('POLYGON').num
        layer = ds.CreateLayer('lyr', srs=output_srs, geom_type=ogr_type)

        attributes = []
        attributes.append("id")
        attributes.append("satellite_instrument_group")
        attributes.append("target_date")

        for field in attributes:
            field_defn = ogr.FieldDefn(str(field), ogr.OFTString)
            field_defn.SetWidth(255)
            if layer.CreateField(field_defn) != 0:
                raise Exception('Faild to create field')

        feature_def = layer.GetLayerDefn()

        for item in recordsArray:
            feat = ogr.Feature(feature_def)

            for field in attributes:
                value = getattr(item, field)
                try:
                    string_value = str(value)
                except UnicodeEncodeError as E:
                    string_value = ''
                feat.SetField(str(field), string_value)

            geom = getattr(item.delivery_detail, "geometry")

            if geom:
                ogr_geom = ogr.CreateGeometryFromWkt(geom.wkt)
                if self.proj_transform:
                    ct = osr.CoordinateTransformation(native_srs, output_srs)
                    ogr_geom.Transform(ct)
                check_err(feat.SetGeometry(ogr_geom))
            else:
                pass

            check_err(layer.CreateFeature(feat))

        ds.Destroy()
        content_type = 'application/zip'
        return self.zip_response(tmp.name, self.file_name, content_type)

    def write_delivery_details(self, theOrder):
        """
          Write a shapefile out to a file from a delivery details clip geometry
        """
        tmp = tempfile.NamedTemporaryFile(suffix='.shp', mode='w+b')
        # we must close the file for GDAL to be able to open and write to it
        tmp.close()

        dr = ogr.GetDriverByName('ESRI Shapefile')
        ds = dr.CreateDataSource(tmp.name)
        if ds is None:
            raise Exception('Could not create file!')

        native_srs = osr.SpatialReference()
        native_srs.ImportFromEPSG(4326)  # latlong wgs84

        if self.proj_transform:
            output_srs = osr.SpatialReference()
            output_srs.ImportFromEPSG(self.proj_transform)
        else:
            output_srs = native_srs

        ogr_type = OGRGeomType('POLYGON').num
        layer = ds.CreateLayer('lyr', srs=output_srs, geom_type=ogr_type)

        #order attributes
        attributes = []
        attributes.append("user")
        attributes.append("notes")
        attributes.append("delivery_method")
        attributes.append("order_date")

        for field in attributes:
            field_defn = ogr.FieldDefn(str(field[0:10]), ogr.OFTString)
            field_defn.SetWidth(255)
            if layer.CreateField(field_defn) != 0:
                raise Exception('Faild to create field')

        feature_def = layer.GetLayerDefn()

        feat = ogr.Feature(feature_def)

        for field in attributes:
            value = getattr(theOrder, field)
            try:
                string_value = str(value)
            except UnicodeEncodeError:
                string_value = ''
            feat.SetField(str(field[0:10]), string_value)

            try:
                string_value = str(value)
            except UnicodeEncodeError:
                string_value = ''
            feat.SetField(str(field[0:10]), string_value)

        check_err(layer.CreateFeature(feat))

        ds.Destroy()
        content_type = 'application/zip'
        return self.zip_response(tmp.name, self.file_name, content_type)

    def write_order_products(self, theRecordsArray):
        """
          Write a shapefile out to a file from a ordered products geometry
        """
        tmp = tempfile.NamedTemporaryFile(suffix='.shp', mode='w+b')
        # we must close the file for GDAL to be able to open and write to it
        tmp.close()

        dr = ogr.GetDriverByName('ESRI Shapefile')
        ds = dr.CreateDataSource(tmp.name)
        if ds is None:
            raise Exception('Could not create file!')

        native_srs = osr.SpatialReference()
        native_srs.ImportFromEPSG(4326)  # latlong wgs84

        if self.proj_transform:
            output_srs = osr.SpatialReference()
            output_srs.ImportFromEPSG(self.proj_transform)
        else:
            output_srs = native_srs

        ogr_type = OGRGeomType('POLYGON').num
        layer = ds.CreateLayer('lyr', srs=output_srs, geom_type=ogr_type)

        attributes = []
        attributes.append("product_id")
        attributes.append("satellite")
        attributes.append("instrument_type")
        attributes.append("product_profile")
        attributes.append("processing_level")
        attributes.append("owner")
        attributes.append("license")
        attributes.append("product_acquisition_start")
        attributes.append("product_acquisition_end")
        attributes.append("projection")
        attributes.append("quality")
        attributes.append("geometric_accuracy_mean")
        attributes.append("geometric_accuracy_1sigma")
        attributes.append("geometric_accuracy_2sigma")
        attributes.append("spectral_accuracy")
        attributes.append("radiometric_signal_to_noise_ratio")
        attributes.append("radiometric_percentage_error")
        attributes.append("spatial_resolution_x")
        attributes.append("spatial_resolution_y")
        attributes.append("spectral_resolution")
        attributes.append("radiometric_resolution")
        attributes.append("creating_software")
        attributes.append("original_product_id")
        attributes.append("orbit_number")
        attributes.append("product_revision")
        attributes.append("path")
        attributes.append("path_offset")
        attributes.append("row")
        attributes.append("row_offset")

        for field in attributes:
            field_defn = ogr.FieldDefn(str(field), ogr.OFTString)
            field_defn.SetWidth(255)
            if layer.CreateField(field_defn) != 0:
                raise Exception('Faild to create field')

        feature_def = layer.GetLayerDefn()

        for item in theRecordsArray:
            feat = ogr.Feature(feature_def)

            for field in attributes:
                #ABP: added getConcreteProduct and None
                value = getattr(
                    item.product.getConcreteInstance(), field, None)
                logger.info("Shape writer: Setting %s to %s" % (field, value))
                try:
                    string_value = str(value)
                except UnicodeEncodeError as E:
                    string_value = ''
                    logger.info("Unicode conversion error")
                #truncate field name to 10 letters to deal with shp limitations
                logger.info("Truncated field name: %s" % str(field[0:10]))
                feat.SetField(str(field[0:10]), string_value)

            geom = getattr(item.product, "spatial_coverage")

            if geom:
                ogr_geom = ogr.CreateGeometryFromWkt(geom.wkt)
                if self.proj_transform:
                    ct = osr.CoordinateTransformation(native_srs, output_srs)
                    ogr_geom.Transform(ct)
                check_err(feat.SetGeometry(ogr_geom))
            else:
                pass

            check_err(layer.CreateFeature(feat))

        ds.Destroy()
        # Next line for debugging only if you want to see log info in
        # debugtoolbar
        # return HttpResponse("<html><head></head><body>Done</body></html>")
        content_type = 'application/zip'
        return self.zip_response(tmp.name, self.file_name, content_type)
