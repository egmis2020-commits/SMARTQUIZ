/* EGMIS V24.6.6
   The page templates already contain the complete, styled navigation header.
   Do not inject or transform another header here: doing so can create a second
   header in the packaged Windows EXE when its cached/static assets differ.
   Keep this file as a compatibility placeholder for templates that still load it. */
(function () {
  'use strict';
  // Intentionally no-op. The existing .app-header and .admin-top-nav markup
  // is styled directly by /static/style.css and should appear exactly once.
})();
