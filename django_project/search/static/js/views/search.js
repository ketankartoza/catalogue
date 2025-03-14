define([
    'backbone',
    'underscore',
    'shared',
    'ol',
    'noUiSlider',
    'jquery',
    'bootstrap',
    'views/date_range',
    'jqueryForm',
    'listTree',
    'datetimepicker',
], function (Backbone, _, Shared, ol, NoUiSlider, $) {

    return Backbone.View.extend({
        template: _.template($('#map-search-container').html()),
        searchBox: null,
        searchBoxOpen: false,
        resultPanelState : false,
        shouldUpdateUrl: true,
        searchResults: {},
        events: {
            'keyup #search': 'checkSearch',
            'keypress #search': 'searchEnter',
            'click .search-arrow': 'searchClick',
            'click .search-reset': 'clearSearch',
            'click #search_button': 'submitSearchForm',
            'click #reset-search-form': 'resetSearchForm',
            'click #logIn': 'login',
            'change #id_geometry_file': 'upload_image',
        },
        initialize: function (options) {
            _.bindAll(this, 'render');
            this.parent = options.parent;
            Shared.Dispatcher.on('search:clearSearch', this.clearSearch, this);
            if(history){
                Shared.Dispatcher.trigger('collectionSearch', {
                        offset: 0
                    });
            }
        },
        render: function () {
            var self = this;
            this.$el.html(this.template());
            this.searchBox = this.$el.find('.map-search-box');
            this.$el.find('.listTree').listTree( listTreeOptions , { "startCollapsed": true, "selected": selectedOptions });

            this.$el.find('#start-datepicker').datepicker({
                autoclose: true,
                startView: 2,
                format: 'dd/mm/yyyy',
                endDate: new Date()
            });
            this.$el.find('#end-datepicker').datepicker({
                autoclose: true,
                startView: 2,
                format: 'dd/mm/yyyy',
                endDate: new Date(),
            });

           this.$el.find('#end_datepicker').datepicker('setDate', new Date())
             // setup daterange widget
            this.$el.find('#date_range').daterange();
            this.$el.find('#date_range').daterange('notify');
            return this;
        },

        initCloudCover: function () {
            // render slider
            const input0 = this.$el.find('#cloud_min');
            const input1 = this.$el.find('#cloud_max');
            const inputs = [input0, input1];

            this.cloudCover = NoUiSlider.create($('#cloud-cover-slider')[0], {
                start: [0, 100],
                connect: true,
                range: {
                    'min': 0,
                    '10%': 10,
                    '20%': 20,
                    '30%': 30,
                    '40%': 40,
                    '50%': 50,
                    '60%': 60,
                    '70%': 70,
                    '90%': 90,
                    '80%': 80,
                    'max': 100
                }
            });

            this.cloudCover.on('update', function (values, handle) {
                inputs[handle].val(values[handle]);
            });
        },

        show: function () {
            this.searchBox.show();
            this.$el.find('#search').focus();
            this.searchBoxOpen = true;
        },
        hide: function () {
            this.searchBox.hide();
            this.searchBoxOpen = false;
        },
        isOpen: function () {
            return this.searchBoxOpen;
        },

        validate_form: function(){
            let form_ok = false;
            const myDateRange = $('#date_range .date_range_row');
            console.log(myDateRange)
            if (myDateRange.length === 0) {
                const helpElem = '<div id="error-date-range" class="alert alert-danger" role="alert">You have to select at least 1 date range!</div>';
                $('#search-panel').animate({ scrollTop: $('#date-range-content').position().top}, 400);
                if(this.$el.find('#error-date-range').length==0){
                     $('#date_range').parent().prepend(helpElem);
                }
              } else {
                if(this.$el.find('#error-date-range').length > 0){
                     $('#error-date-range').remove();
                }
                form_ok = true;
            }
              return form_ok;
        },
        resetSearchFromErrors: function() {
            /* remove all error notifciatons on search form */
            $('.error-block').each( function() { this.remove(); })
            $('.error').each( function() { $(this).removeClass('error'); })
        },

        processSearchFormErrors: function(data) {
            /* process json with errors when search submit fails
            set class error to control-group div
            add span element that holds error message afer input */
            const self = this
            self.resetSearchFromErrors();
            const errors = $.parseJSON(data);
            for (const field in errors) {
                const inputDOM = $('#id_' + field);
                inputDOM.parent().parent().addClass('error');
                const helpElem = '<div class="alert alert-danger" role="alert">' + errors[field] + '</div>';
                inputDOM.parent().append(helpElem);
            }
            $('.error-block').first().closest('.accordion-body').collapse('show');
        },

        submitSearchForm: function () {
            const self = this;
            $('#catalogueSearch').ajaxForm({
                type: 'POST',
                dataType: 'json',
                beforeSubmit: function(formData, jqForm, options) {
                    if (self.validate_form()) {
                        // process data if needed... before submit
                        const selected_sensors = [];
                        _.each($('.listTree').data('listTree').selected, function(parent) {
                          _.each(parent.values, function(sensor) {
                            selected_sensors.push(sensor.val);
                          });
                        });

                        _.each(formData, function (element, index) {
                          if (element.name === 'selected_sensors') {
                            // update selected sensors value
                            formData[index].value = selected_sensors;
                          }
                        });

                    } else {
                        // don't submit the form, there is an error in JS form validation
                        return false;
                      }
                    },
                success: function(data){
                    self.resetSearchFromErrors();
                    guid = data.guid;
                    Shared.Dispatcher.trigger('collectionSearch', {
                        offset: 0
                    });

                    // set redirect link if user is not loged in
                    // if he logs in, he will be redirected  back to last performed search

                    if (!UserLoged) {
                        var link = $('#loginLink').attr("href") + '?next=/search/' + guid + '/';
                        var popLink = $('#logIn').attr("href") + '?next=/search/' + guid + '/';
                        $('#loginLink').attr("href", link);
                        $('#logIn').attr("href", popLink);
                        link = $('#loginRegister').attr("href") + '?next=/search/' + guid + '/';
                        $('#loginRegister').attr("href", link);
                    }

                    },
                    error: function(data) {
                        if (data.status == '404') {
                            self.processSearchFormErrors(data.responseText);
                        } else {
                            alert('Sorry! There has been an error. Please try again');
                            console.log(data);
                        }
                    }

                });
                // submit the form
                $('#catalogueSearch').submit();

        },

        resetSearchFromErrors: function() {
            /* remove all error notifciatons on search form */
            $('.error-block').each( function() { this.remove(); })
            $('.error').each( function() { $(this).removeClass('error'); })
        },

        resetSearchForm: function() {
            // reset listTree
            const self = this;
            $('.listTree').listTree('deselectAll');
            // remove dateranges
            $('#date_range').daterange('reset');
            // reset text fields
            $('input:text').each(function() {
                $(this).val('');
            });
            // set cloud back to 100
            $('#id_cloud_min').val(0);
            $('#id_cloud_max').val(100);
            // clear checkboxes
            $('input:checkbox').each(function() {
                $(this).attr('checked', false);
            });
            // reset dropdowns
            $('#id_spatial_resolution').prop('selectedIndex',0);
            $('#id_band_count').prop('selectedIndex',0);

            // reset file filed
            file = $('#id_geometry_file');
            file.val("");
            file.replaceWith( file = file.clone( true ) );

            // reset search summary widget
            // searchSummary.reset();
            self.resetSearchFromErrors();
        },

        upload_image: function() {
            const field = $('#id_geometry_file');
            const upload_url = '/upload_geo/'
            if (field.get(0).files.length === 0) {
                return;
            }
            let file = field.get(0).files[0];
            const formdata = new FormData();
            formdata.append('file_upload', file);
            $.ajax({
                url: upload_url,
                headers: {"X-CSRFToken": csrfToken},
                type: 'POST',
                data: formdata,
                processData: false,
                contentType: false,
                success: function(data) {
                    Shared.Dispatcher.trigger('map:clearAoiBounds')
                    Shared.Dispatcher.trigger('map:drawWKT', data)
                },
                error: function(data, text) {
                    alert($.parseJSON(data.responseText).error);
                }
            });
        },


    })

});
