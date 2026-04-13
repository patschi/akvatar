/**
 * Tooltip system – converts native title attributes to data-tooltip so
 * CSS-only tooltips can show instantly (bypassing the browser's built-in delay).
 */
(function () {
  function convertTitles(root) {
    root.querySelectorAll("[title]").forEach(function (el) {
      var text = el.getAttribute("title");
      if (text) {
        el.setAttribute("data-tooltip", text);
        el.removeAttribute("title");
      }
    });
  }

  // Convert existing elements once the DOM is ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { convertTitles(document); });
  } else {
    convertTitles(document);
  }

  // Watch for dynamically added elements with title attributes
  new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      m.addedNodes.forEach(function (node) {
        if (node.nodeType === 1) convertTitles(node);
      });
    });
  }).observe(document.documentElement, { childList: true, subtree: true });
})();

