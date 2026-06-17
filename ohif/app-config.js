window.config = {
  // OHIF is served behind the platform nginx under the /ohif/ subpath (see
  // nginx/nginx.conf). React Router must use this basename so that the browser
  // path /ohif/viewer resolves to OHIF's internal /viewer route — otherwise the
  // app mounts but no route matches and the viewer renders blank/black.
  // NOTE: this must be set explicitly; OHIF only falls back to window.PUBLIC_URL
  // when routerBasename is unset (`appConfig.routerBasename ||= publicUrl`).
  routerBasename: '/ohif/',
  showStudyList: true,
  // OHIF's appInit does `[...defaultExtensions, ...appConfig.extensions]`, so
  // these MUST be present (and iterable) even when empty — the built-in
  // extensions/modes are bundled at build time and registered regardless.
  // Omitting them throws "appConfig.extensions is not iterable" at boot and the
  // viewer renders blank.
  extensions: [],
  modes: [],
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'orthanc',
      configuration: {
        friendlyName: 'Orthanc PACS',
        name: 'orthanc',
        wadoUriRoot: 'http://localhost/wado',
        qidoRoot: 'http://localhost/dicom-web',
        wadoRoot: 'http://localhost/dicom-web',
        qidoSupportsIncludeField: false,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        bulkDataURI: {
          enabled: true,
        },
      },
    },
  ],
  defaultDataSourceName: 'orthanc',
};
