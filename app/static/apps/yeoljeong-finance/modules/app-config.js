(() => {
  const config = {
    appName: "매장비서",
    updatedAt: "2026-07-16 11:23:56 KST",
    modulePhase: "phase-1-manifest",
    docs: {
      index: "/static/reports/20260716_yeoljeong_store_assistant_docs_index.html",
      technical: "/static/reports/20260716_yeoljeong_store_assistant_technical.html",
      architecture: "/static/reports/20260716_yeoljeong_store_assistant_architecture_design_plan.html",
      dbTransition: "/static/reports/20260716_yeoljeong_store_assistant_db_transition_plan.html"
    },
    modules: [
      { key: "auth", owner: "inline", target: "modules/auth-session.js" },
      { key: "settings", owner: "inline", target: "modules/settings-workspace.js" },
      { key: "employee", owner: "inline", target: "modules/employee-workflow.js" },
      { key: "contracts", owner: "inline", target: "modules/contracts-a4.js" },
      { key: "payroll", owner: "inline", target: "modules/payroll-statements.js" },
      { key: "delivery", owner: "inline", target: "modules/delivery-collection.js" }
    ]
  };

  window.YEOLJEONG_STORE_ASSISTANT_CONFIG = Object.freeze(config);

  document.addEventListener("DOMContentLoaded", () => {
    document.body.dataset.frontendModulePhase = config.modulePhase;
    document.querySelectorAll("[data-doc-key]").forEach(link => {
      const key = link.getAttribute("data-doc-key");
      if (key && config.docs[key]) link.setAttribute("href", config.docs[key]);
    });
  });
})();
