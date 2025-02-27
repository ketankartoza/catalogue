"""
SANSA-EO Catalogue - Initialization, generic and helper methods

Contact : lkleyn@sansa.org.za

.. note:: This program is the property of the South African National Space
   Agency (SANSA) and may not be redistributed without expresse permission.
   This program may include code which is the intellectual property of
   Linfiniti Consulting CC. Linfiniti grants SANSA perpetual, non-transferrable
   license to use any code contained herein which is the intellectual property
   of Linfiniti Consulting CC.

"""

__author__ = 'tim@linfiniti.com'
__version__ = '0.1'
__date__ = '01/01/2011'
__copyright__ = 'South African National Space Agency'

# for kmz
import zipfile
from io import BytesIO
import os.path
import re
from email.mime.base import MIMEBase

import logging

from django.core.files.storage import FileSystemStorage
from django.template import RequestContext
# for rendering template to email
from django.template.loader import render_to_string
# for sending email
from django.core import mail
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, SafeMIMEMultipart

from django.contrib.auth.decorators import login_required
from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponseRedirect, HttpResponse

from orders.models import (
    Order,
    OrderStatusHistory,
    OrderNotificationRecipients
)

from search.models import SearchRecord
from weasyprint import HTML

# Read default notification recipients from settings
CATALOGUE_DEFAULT_NOTIFICATION_RECIPIENTS = getattr(
    settings,
    'CATALOGUE_DEFAULT_NOTIFICATION_RECIPIENTS', False)

###########################################################
#
# EmailMessage subclass that makes it easy to send multipart/related
# messages. For example, including text and HTML versions with inline images.
#
# courtesy of: http://www.cupcakewithsprinkles.com/html-emails-with-inline
#                     -images-in-django/
#
###########################################################
logger = logging.getLogger(__name__)


class EmailMultiRelated(EmailMultiAlternatives):
    """
    A version of EmailMessage that makes it easy to send multipart/related
    messages. For example, including text and HTML versions with inline images.
    """
    related_subtype = 'related'

    def __init__(
            self, subject='', body='', from_email=None, to=None, bcc=None,
            connection=None, attachments=None, headers=None,
            alternatives=None):
        # self.related_ids = []
        self.related_attachments = []
        super(EmailMultiRelated, self).__init__(
            subject, body, from_email, to, bcc, connection, attachments,
            headers, alternatives)

    def attach_related(self, filename=None, content=None, content_type=None):
        """
        Attaches a file with the given filename and content. The filename can
        be omitted and the content_type is guessed, if not provided.

        If the first parameter is a MIMEBase subclass it is inserted directly
        into the resulting message attachments.
        """

        if isinstance(filename, MIMEBase):
            assert content == content_type is None
            self.related_attachments.append(filename)
        else:
            assert content is not None
            self.related_attachments.append((filename, content, content_type))

    def attach_related_file(self, path, content_type=None):
        """Attaches a file from the filesystem."""
        filename = os.path.basename(path)
        content = open(path, 'rb').read()
        self.attach_related(filename, content, content_type)

    def _create_message(self, msg):
        return self._create_attachments(
            self._create_related_attachments(self._create_alternatives(msg)))

    def _create_alternatives(self, msg):
        for i, (content, content_type) in enumerate(self.alternatives):
            if content_type == 'text/html':
                for filename, _, _ in self.related_attachments:
                    content = re.sub(r'(?<!cid:)%s' % re.escape(filename),
                                     'cid:%s' % filename, content)
                    self.alternatives[i] = (content, content_type)

            return super(EmailMultiRelated, self)._create_alternatives(msg)

    def _create_related_attachments(self, msg):
        encoding = self.encoding or settings.DEFAULT_CHARSET
        if self.related_attachments:
            body_msg = msg
            msg = SafeMIMEMultipart(_subtype=self.related_subtype,
                                    encoding=encoding)
            if self.body:
                logging.error(msg)
                msg.attach(body_msg)
                for related in self.related_attachments:
                    msg.attach(self._create_related_attachment(*related))
        return msg

    def _create_related_attachment(self, filename, content, content_type):
        """
        Convert the filename, content, content_type triple into a MIME attachment
        object. Adjust headers to use Content-ID where applicable.
        Taken from http://code.djangoproject.com/ticket/4771
        """
        content_type = "multipart/related"
        attachment = super(EmailMultiRelated, self)._create_attachment(
            filename, content, content_type)
        if filename:
            content_type = attachment['Content-Type']
            del (attachment['Content-Type'])
            del (attachment['Content-Disposition'])
            attachment.add_header('Content-Disposition', 'inline',
                                  filename=filename)
            attachment.add_header('Content-Type', content_type, name=filename)
            attachment.add_header('Content-ID', '<%s>' % filename)
        return attachment


###########################################################
#
# Object duplication generic code
# from: http://github.com/johnboxall/django_usertools/blob/
#            28c1f243a4882da1e63b60d54a86947db4847cf6/helpers.py#L23
#
# This code could be used to duplicate the Search object
#
###########################################################


from django.db.models.deletion import Collector
from django.db.models.fields.related import ForeignKey


def update_related_field(obj, value, field):
    """
    Set `field` to `value` for all objects related to `obj`.
    Based on heavily off the delete object code:
    http://code.djangoproject.com/browser/django/trunk/django/db/models
                    /query.py#L824
    """
    # Collect all related objects.
    collected_objs = Collector()
    obj._collect_sub_objects(collected_objs)
    classes = list(collected_objs.keys())
    # Bulk update the objects for performance
    for cls in classes:
        items = list(collected_objs[cls].items())
        pk_list = [pk for pk, instance in items]
        cls._default_manager.filter(id__in=pk_list).update(**{field: value})
        del instance
    return obj


def duplicate(obj, value=None, field=None, duplicate_order=None):
    """
    Duplicate all related objects of obj setting
    field to value. If one of the duplicate
    objects has an FK to another duplicate object
    update that as well. Return the duplicate copy
    of obj.

    duplicate_order is a list of models which specify how
    the duplicate objects are saved. For complex objects
    this can matter. Check to save if objects are being
    saved correctly and if not just pass in related objects
    in the order that they should be saved.

    """
    collected_objs = Collector()
    obj._collect_sub_objects(collected_objs)
    related_models = list(collected_objs.keys())
    root_obj = None

    # Sometimes it's good enough just to save in reverse deletion order.
    if duplicate_order is None:
        duplicate_order = reversed(related_models)

    for model in duplicate_order:
        # Find all FKs on model that point to a related_model.
        fks = []
        for f in model._meta.fields:
            if isinstance(f, ForeignKey) and f.rel.to in related_models:
                fks.append(f)
        # Replace each `sub_obj` with a duplicate.
        if model not in collected_objs:
            continue
        sub_obj = collected_objs[model]
        for pk_val, obj in sub_obj.items():
            for fk in fks:
                fk_value = getattr(obj, '%s_id' % fk.name)
                # If this FK has been duplicated then point to the duplicate.
                if fk_value in collected_objs[fk.rel.to]:
                    dupe_obj = collected_objs[fk.rel.to][fk_value]
                    setattr(obj, fk.name, dupe_obj)
            # Duplicate the object and save it.
            obj.id = None
            if field is not None and value is not None:
                setattr(obj, field, value)
            obj.save()
            if root_obj is None:
                root_obj = obj
            # unused
            del pk_val
    return root_obj


###########################################################
#
# Email notification of orders to SANSA sales staff
#
###########################################################


def notify_sales_staff(theUser, theOrderId, theContext=None):
    """
    A helper method to notify sales staff who are subscribed to a sensor
    Example usage from the console / doctest:

    Args:
        theUser obj - Required. Django user object
        theOrderId int - Required. ID of the Order which has changed
        theContext obj - Optional. Useful when we need to pass RequestContext
            to the render_to_string (see Note)

    Note:
        RequestContext is important when executing unittests using test.client
        because of the way test.client generates response context object.
        Response context is a list of context objects, one for each used
        template, and if during rendering of a template context object is None,
        values of response context object are going to be empty. For example
        myResp.context['myObjects'] = [], but in reality it should have values
    """

    if not settings.EMAIL_NOTIFICATIONS_ENABLED:
        logger.info('Email sending disabled, set EMAIL_NOTIFICATIONS_ENABLED '
                    'in settings')
        return
    order = get_object_or_404(Order, id=theOrderId)
    records = SearchRecord.objects.filter(user=theUser,
                                          order=order).select_related()
    history = OrderStatusHistory.objects.filter(order=order)
    dictionary = {
        'myOrder': order,
        'myRecords': records,
        'myHistory': history
    }
    html_template = 'pdf/order-summary.html'
    html_string = render_to_string(html_template, dictionary)
    html_object = HTML(
        string=html_string,
    )
    html_object.write_pdf(target='/tmp/order-summary.pdf')
    email_subject = ('SANSA Order ' + str(order.id) + ' status update (' +
                     order.order_status.name + ')')

    # Get a list of staff user's email addresses
    # we will use mass_mail to prevent users seeing who other recipients are
    messages = []

    recipients = set()
    recipients.update([theUser])
    logger.info('User recipient added: %s' % str(recipients))
    # get the list of recipients
    for product in [s.product for s in records]:
        recipients.update(
            OrderNotificationRecipients.get_users_for_product(product))

    # Add default recipients
    if not recipients and CATALOGUE_DEFAULT_NOTIFICATION_RECIPIENTS:
        logger.info('Sending notice to default recipients : %s' %
                    CATALOGUE_DEFAULT_NOTIFICATION_RECIPIENTS)
        recipients.update(list(CATALOGUE_DEFAULT_NOTIFICATION_RECIPIENTS))

    recipients.add(settings.EMAIL_CUSTOMER_SUPPORT)

    for recipient in recipients:
        # txt email template
        email_message_txt = render_to_string(
            'mail/order.txt', {
                'myOrder': order,
                'myRecords': records,
                'myHistory': history,
                'myRecipient': recipient,
                'domain': settings.DOMAIN
            }, theContext)
        # html email template
        email_message_html = render_to_string(
            'mail/order.html', {
                'myOrder': order,
                'myRecords': records,
                'myHistory': history,
                'myRecipient': recipient,
                'domain': settings.DOMAIN
            }, theContext)
        address = recipient.email if hasattr(recipient, 'email') else recipient
        msg = EmailMultiRelated(
            email_subject,
            email_message_txt,
            'dontreply@' + settings.DOMAIN, [address])
        logger.info('Sending notice to : %s' % address)

        # attach alternative payload - html
        msg.attach_alternative(email_message_html, 'text/html')
        # add required images, as inline attachments,
        # accessed by 'name' in templates
        msg.attach_related_file(
            os.path.join(
                settings.STATIC_ROOT, 'images', 'sac_header_email.jpg'))
        # get the filename of a PDF, ideally we should reuse theOrderPDF object

        msg.attach_related_file('/tmp/order-summary.pdf')
        # add message
        messages.append(msg)

    logger.info('Sending messages: \n%s' % messages)
    # initiate email connection, and send messages in bulk
    email = mail.get_connection()
    email.send_messages(messages)
    return


"""Layer definitions for use in conjunction with open layers"""
WEB_LAYERS = {
    'TMSOverlay':
        """var tmsoverlay = new OpenLayers.Layer.TMS(
                "TMS Overlay", "", {   // url: '', serviceVersion: '.',
                layername: '.', type: 'png', getURL: overlay_getTileURL,
                alpha: true, isBaseLayer: false
                });
                if (OpenLayers.Util.alphaHack() == false)
                { tmsoverlay.setOpacity(0.7); }
                """,

    'ZaRoadsBoundaries':
        """var zaRoadsBoundaries = new OpenLayers.Layer.WMS(
               'SA Vector', 'http://""" + settings.WMS_SERVER +
        """/cgi-bin/tilecache.cgi?',
          {
             VERSION: '1.1.1',
             EXCEPTIONS: 'application/vnd.ogc.se_inimage',
             width: '800',
             //layers: 'Roads',
             layers: 'za_vector',
             srs: 'EPSG:900913',
             maxResolution: '156543.0339',
             height: '525',
             format: 'image/jpeg',
             transparent: 'false',
             antialiasing: 'true'
           },
           {isBaseLayer: true});
           """,
    # Map of all search footprints that have been made.
    # Transparent: true will make a wms layer into an overlay
    'Searches': """var searches = new OpenLayers.Layer.WMS(
                'Searches', 'http://""" + settings.WMS_SERVER +
                """/cgi-bin/mapserv?map=SEARCHES',
          {
             VERSION: '1.1.1',
             EXCEPTIONS: 'application/vnd.ogc.se_inimage',
             width: '800',
             layers: 'searches',
             srs: 'EPSG:900913',
             maxResolution: '156543.0339',
             height: '525',
             format: 'image/png',
             transparent: 'true'
           },
           {isBaseLayer: false});
           """,
    # Map of site visitors
    # Transparent: true will make a wms layer into an overlay
    'Visitors': """var visitors = new OpenLayers.Layer.WMS(
          'Visitors', 'http://""" + settings.WMS_SERVER +
                """/cgi-bin/mapserv?map=VISITORS',
          {
             VERSION: '1.1.1',
             EXCEPTIONS: 'application/vnd.ogc.se_inimage',
             width: '800',
             layers: 'visitors',
             styles: '',
             srs: 'EPSG:900913',
             maxResolution: '156543.0339',
             height: '525',
             format: 'image/png',
             transparent: 'true'
           },
           {isBaseLayer: false}
       );
        """,
    # Nasa Blue marble directly from mapserver
    'BlueMarble':
        """var BlueMarble = new OpenLayers.Layer.WMS('BlueMarble',
                'http://""" + settings.WMS_SERVER +
        """/cgi-bin/mapserv?map=WORLD',
            {
             VERSION: '1.1.1',
             EXCEPTIONS: 'application/vnd.ogc.se_inimage',
             layers: 'BlueMarble',
             maxResolution: '156543.0339'
            });
            BlueMarble.setVisibility(false);
            """,
    #
    # Google
    #
    'GooglePhysical': """var gphy = new OpenLayers.Layer.Google(
           'Google Physical',
           {type: G_PHYSICAL_MAP}
          );
       """,
    #
    # Google streets
    #
    'GoogleStreets': """var gmap = new OpenLayers.Layer.Google(
           'Google Streets' // the default
          );
        """,
    #
    # Google hybrid
    #
    'GoogleHybrid': """ var ghyb = new OpenLayers.Layer.Google(
           'Google Hybrid',
           {type: G_HYBRID_MAP}
          );
        """,
    #
    # Google Satellite
    #
    'GoogleSatellite': """var gsat = new OpenLayers.Layer.Google(
           'Google Satellite',
           {type: G_SATELLITE_MAP}
          );
        """,
    #
    # Heatmap - all
    #
    'Heatmap-total': """var heatmap_total = new OpenLayers.Layer.Image(
                'Heatmap total',
                '/media/heatmaps/heatmap-total.png',
                new OpenLayers.Bounds(-20037508.343,
                                      -16222639.241,
                                      20019734.329,
                                      16213801.068),
                new OpenLayers.Size(1252,1013),
                {isBaseLayer: true,
                maxResolution: 156543.0339
                }
           );
        """,
    #
    # Heatmap - last3month
    #
    'Heatmap-last3month':
        """var heatmap_last3month = new OpenLayers.Layer.Image(
                'Heatmap last 3 months',
                '/media/heatmaps/heatmap-last3month.png',
                new OpenLayers.Bounds(-20037508.343,
                                      -16222639.241,
                                      20019734.329,
                                      16213801.068),
                new OpenLayers.Size(1252,1013),
                {isBaseLayer: true,
                maxResolution: 156543.0339
                }
           );
        """,
    #
    # Heatmap - last month
    #
    'Heatmap-lastmonth':
        """var heatmap_lastmonth = new OpenLayers.Layer.Image(
                'Heatmap last month',
                '/media/heatmaps/heatmap-lastmonth.png',
                new OpenLayers.Bounds(-20037508.343,
                                      -16222639.241,
                                      20019734.329,
                                      16213801.068),
                new OpenLayers.Size(1252,1013),
                {isBaseLayer: true,
                maxResolution: 156543.0339
                }
           );
        """,
    #
    # Heatmap - last week
    #
    'Heatmap-lastweek':
        """var heatmap_lastweek = new OpenLayers.Layer.Image(
                'Heatmap last week',
                '/media/heatmaps/heatmap-lastweek.png',
                new OpenLayers.Bounds(-20037508.343,
                                      -16222639.241,
                                      20019734.329,
                                      16213801.068),
                new OpenLayers.Size(1252,1013),
                {isBaseLayer: true,
                maxResolution: 156543.0339
                }
           );
        """,
    # Note for this layer to be used you need to regex replace
    # USERNAME with theRequest.user.username
    'CartLayer':
        """var cartLayer = new OpenLayers.Layer.WMS('Cart', 'http://""" +
        settings.WMS_SERVER + """/cgi-bin/mapserv?map=""" +
        settings.CART_LAYER + """&user=USERNAME',
          {
             version: '1.1.1',
             width: '800',
             layers: 'Cart',
             srs: 'EPSG:4326',
             height: '525',
             format: 'image/png',
             transparent: 'true'
           },
           {isBaseLayer: false});
           """,
}

mLayerJs = {
    'VirtualEarth': (
        '<script src="http://dev.virtualearth.net/mapcontrol/mapcontrol.'
        'ashx?v=6.1"></script>'),
    'Google': (
        '<script src="http://maps.google.com/maps?file=api&amp;v=2&amp;'
        'key="{{GOOGLE_MAPS_API_KEY}}"></script>')
}


# Note this code is from Tims personal codebase and copyright is retained
@login_required
def genericAdd(
        theRequest,
        theFormClass,
        theTitle,
        theRedirectPath,
        theOptions):
    myObject = getObject(theFormClass)
    logger.info('Generic add called')
    if theRequest.method == 'POST':
        # create a form instance using reflection
        # see http://stackoverflow.com/questions/452969/does-python-have-an
        #         -equivalent-to-java-class-forname/452981
        myForm = myObject(theRequest.POST, theRequest.FILES)
        myOptions = {
            'myForm': myForm,
            'myTitle': theTitle
        }
        # shortcut to join two dicts
        myOptions.update(theOptions),
        if myForm.is_valid():
            myObject = myForm.save(commit=False)
            myObject.user = theRequest.user
            myObject.save()
            logger.info('Add : data is valid')
            return HttpResponseRedirect(theRedirectPath + str(myObject.id))
        else:
            logger.info('Add : form is NOT valid')
            return render_to_response(
                'add.html', myOptions,
                context_instance=RequestContext(theRequest))
    else:
        myForm = myObject()
        myOptions = {
            'myForm': myForm,
            'myTitle': theTitle
        }
        # shortcut to join two dicts
        myOptions.update(theOptions),
        logger.info('Add : new object requested')
        return render_to_response(
            'add.html', myOptions,
            context_instance=RequestContext(theRequest))


def genericDelete(theRequest, theObject):
    if theObject.user != theRequest.user:
        return ({'myMessage': 'You can only delete an entry that you own!'})
    else:
        theObject.delete()
        return {'myMessage': 'Entry was deleted successfully'}


def getObject(theClass):
    # Create an object instance using reflection
    # from http://stackoverflow.com/questions/452969/does-python-have-an
    #           -equivalent-to-java-class-forname/452981
    myParts = theClass.split('.')
    myModule = '.'.join(myParts[:-1])
    myObject = __import__(myModule)
    for myPath in myParts[1:]:
        myObject = getattr(myObject, myPath)
    return myObject


@login_required
def isStrategicPartner(request):
    """Returns true if the current user is a CSIR strategic partner
    otherwise false"""
    myProfile = None
    try:
        myProfile = request.user.get_profile()
    except:
        logger.debug('Profile does not exist')
    myPartnerFlag = False
    if myProfile and myProfile.strategic_partner:
        myPartnerFlag = True
    return myPartnerFlag


def standardLayers(request):
    """Helper methods used to return standard layer defs for the openlayers
       control
       .. note:: intended to be published as a view in urls.py

      e.g. usage:
      myLayersList, myLayerDefinitions, myActiveLayer =
           standardLayers(theRequest)
      where:
        myLayersList will be a string representing a javascript array of layers
        myLayerDefinitions will be an array of strings each representing
            javascript / openlayers layer defs
        myActiveLayer will be the name of the active base map
      """

    myProfile = None
    myLayersList = None
    myLayerDefinitions = None
    myActiveBaseMap = None
    try:
        myProfile = request.user.get_profile()
    except:
        logger.debug('Profile does not exist')
    if myProfile and myProfile.strategic_partner:
        myLayerDefinitions = [
            WEB_LAYERS['TMSOverlay']]
        myLayersList = ('[TMSOverlay]')
        myActiveBaseMap = 'TMSOverlay'
    else:
        myLayerDefinitions = [
            WEB_LAYERS['TMSOverlay']]
        myLayersList = ('[TMSOverlay]')
        myActiveBaseMap = 'TMSOverlay'
    return myLayersList, myLayerDefinitions, myActiveBaseMap


def standardLayersWithCart(request):
    """Helper methods used to return standard layer defs for the openlayers
       control
       .. note:: intended to be published as a view in urls.py
       Note. Appends the cart layer to the list of layers otherwise much the
       same as standardLayers method
       e.g. usage:
       myLayersList, myLayerDefinitions, myActiveLayer =
         standardLayers(theRequest)
       where:
        myLayersList will be a string representing a javascript array of layers
        myLayerDefinitions will be an array of strings each representing
          javascript / openlayers layer defs
        myActiveLayer will be the name of the active base map
      """
    (myLayersList,
     myLayerDefinitions, myActiveBaseMap) = standardLayers(request)
    myLayersList = myLayersList.replace(']', ',cartLayer]')
    myLayerDefinitions.append(
        WEB_LAYERS['CartLayer'].replace('USERNAME', request.user.username))
    return myLayersList, myLayerDefinitions, myActiveBaseMap


def writeThumbToZip(theImagePath, theProductId, theZip):
    """Write a thumb and its world file into a zip file.

    Args:
        theImagePath: str required - path to the image file to write. For the
            world file its extension will be replaced with .wld.
        theProductId: str required - product id used as the output file name
            in the zip file.
        theZip: ZipFile required - handle to a ZipFile instance in which the
            images will be written.

    Returns:
        bool: True on success, False on failure

    Raises:
        Exceptions and issues are logged but not raised.
    """

    myWLDFile = '%s.wld' % os.path.splitext(theImagePath)[0]
    try:
        if os.path.isfile(theImagePath):
            with open(theImagePath, 'rb') as myFile:
                theZip.writestr('%s.jpg' % theProductId,
                                myFile.read())
                logger.debug('Adding thumbnail image to archive.')
        else:
            raise Exception('Thumbnail image not found: %s' % theImagePath)
        if os.path.isfile(myWLDFile):
            with open(myWLDFile, 'rb') as myFile:
                theZip.writestr('%s.wld' % theProductId,
                                myFile.read())
                logger.debug('Adding worldfile to archive.')
        else:
            raise Exception('World file not found: %s' % myWLDFile)
    except:
        logger.exception('Error writing thumb to zip')
        return False
    return True


def writeSearchRecordThumbToZip(theSearchRecord, theZip):
    """A helper function to write a thumbnail into a zip file.
    @parameter myRecord - a searchrecord instance
    @parameter theZip - a zip file handle ready to write stuff to
    """
    # Try to add thumbnail + wld file, we assume that jpg and wld
    # file have same name

    myImageFile = theSearchRecord.product.georeferencedThumbnail()
    return writeThumbToZip(myImageFile,
                           theSearchRecord.product.product_id,
                           theZip)


# render_to_kml helpers
def render_to_kml(template, context, filename):

    response = HttpResponse(render_to_string(template, context))
    response['Content-Type'] = 'application/vnd.google-earth.kml+xml'
    response['Content-Disposition'] = 'attachment; filename=%s.kml' % filename
    return response


def render_to_kmz(template, context, filename):
    """Render a kmz file. If search records are supplied, their georeferenced
    thumbnails will be bundled into the kmz archive."""
    # try to get MAX_METADATA_RECORDS from settings, default to 500
    myMaxMetadataRecords = getattr(settings, 'MAX_METADATA_RECORDS', 500)
    logging.error('testtt', context)
    myKml = render_to_string(template, context)
    myZipData = BytesIO()
    myZip = zipfile.ZipFile(myZipData, 'w', zipfile.ZIP_DEFLATED)
    myZip.writestr('%s.kml' % filename, myKml)
    if 'mySearchRecords' in context:
        for myRecord in context['mySearchRecords'][:myMaxMetadataRecords]:
            writeSearchRecordThumbToZip(myRecord, myZip)
    myZip.close()
    response = HttpResponse()
    response.content = myZipData.getvalue()
    response['Content-Type'] = 'application/vnd.google-earth.kmz'
    response['Content-Disposition'] = 'attachment; filename=%s.kmz' % filename
    response['Content-Length'] = str(len(response.content))
    return response


def downloadISOMetadata(theSearchRecords, theName):
    """ returns ZIPed XML metadata files for each product """
    response = HttpResponse()
    myZipData = BytesIO()
    myZip = zipfile.ZipFile(myZipData, 'w', zipfile.ZIP_DEFLATED)
    # try to get MAX_METADATA_RECORDS from settings, default to 500
    myMaxMetadataRecords = getattr(settings, 'MAX_METADATA_RECORDS', 500)
    for mySearchRecord in theSearchRecords[:myMaxMetadataRecords]:
        myMetadata = mySearchRecord.product.getXML()
        logger.info('Adding product XML to ISO Metadata archive.')
        myZip.writestr('%s.xml' % mySearchRecord.product.product_id,
                       myMetadata)
        writeSearchRecordThumbToZip(mySearchRecord, myZip)

    myZip.close()
    response.content = myZipData.getvalue()
    myZipData.close()
    # get ORGANISATION_ACRONYM from settings, default to 'SANSA'
    myOrganisationAcronym = getattr(settings, 'ORGANISATION_ACRONYM', 'SANSA')
    filename = '%s-%s-Metadata.zip' % (myOrganisationAcronym, theName)
    response['Content-Type'] = 'application/zip'
    response['Content-Disposition'] = 'attachment; filename=%s' % filename
    response['Content-Length'] = str(len(response.content))
    return response


def downloadHtmlMetadata(theSearchRecords, theName):
    """ returns ZIPed html metadata files for each product """
    response = HttpResponse()
    myZipData = BytesIO()
    myZip = zipfile.ZipFile(myZipData, 'w', zipfile.ZIP_DEFLATED)
    # try to get MAX_METADATA_RECORDS from settings, default to 500
    myMaxMetadataRecords = getattr(settings, 'MAX_METADATA_RECORDS', 500)
    # used to tell html renderer not to prepend server path
    myThumbIsLocalFlag = True
    for mySearchRecord in theSearchRecords[:myMaxMetadataRecords]:
        myMetadata = mySearchRecord.product.getConcreteInstance().toHtml(
            myThumbIsLocalFlag)
        logger.info('Adding product HTML to HTML Metadata archive.')
        myZip.writestr('%s.html' % mySearchRecord.product.product_id,
                       myMetadata)
        writeSearchRecordThumbToZip(mySearchRecord, myZip)

    myZip.close()
    response.content = myZipData.getvalue()
    myZipData.close()
    # get ORGANISATION_ACRONYM from settings, default to 'SANSA'
    myOrganisationAcronym = getattr(settings, 'ORGANISATION_ACRONYM', 'SANSA')
    filename = '%s-%s-Metadata.zip' % (myOrganisationAcronym, theName)
    response['Content-Type'] = 'application/zip'
    response['Content-Disposition'] = 'attachment; filename=%s' % filename
    response['Content-Length'] = str(len(response.content))
    return response
