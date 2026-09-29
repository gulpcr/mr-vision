// navigator.clipboard only exists in secure contexts (HTTPS / localhost). Over plain
// HTTP it is undefined, and OHIF's "Copy" buttons (e.g. on the error notification)
// crash with "Cannot read properties of undefined (reading 'writeText')". Provide a
// writeText fallback using a hidden textarea + execCommand('copy'). This file loads
// before OHIF's bundle, so the fallback is in place before anything uses it.
(function () {
  if (typeof navigator === 'undefined' || navigator.clipboard) return;
  function writeText(text) {
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = String(text);
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      if (ok) resolve();
      else reject(new Error('Copy to clipboard failed'));
    });
  }
  try {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: writeText }, configurable: true });
  } catch (e) {
    /* leave clipboard undefined */
  }
})();

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
        // Relative (same-origin) roots. OHIF, the UI, and the Orthanc
        // /dicom-web + /wado endpoints are all served by the same platform
        // nginx, so leaving off the scheme+host makes the browser resolve
        // these against whatever origin loaded the viewer. This works over
        // http or https, from localhost, the exposed IP, or a domain name,
        // with no CORS or mixed-content problems. Do NOT hardcode a host here.
        wadoUriRoot: '/wado',
        qidoRoot: '/dicom-web',
        wadoRoot: '/dicom-web',
        qidoSupportsIncludeField: false,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: false,
        supportsWildcard: true,
        bulkDataURI: {
          enabled: true,
          // Orthanc (orthanc.json DicomWeb.Host = "0.0.0.0") advertises bulk data —
          // e.g. overlay graphics (6000,3000) on Siemens "PosDisp" series — as
          // http://0.0.0.0/dicom-web/..., which the browser cannot reach: scrolling onto
          // such an image failed with "request failed". Rewrite to a same-origin path so
          // it goes through the platform nginx (and its DICOMweb authorisation).
          startsWith: 'http://0.0.0.0/',
          prefixWith: '/',
        },
      },
    },
  ],
  defaultDataSourceName: 'orthanc',
};
