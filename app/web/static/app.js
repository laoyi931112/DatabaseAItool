(function () {
  "use strict";

  var typeSelect = document.getElementById("db-type");
  var portInput = document.getElementById("db-port");
  var databaseLabel = document.getElementById("database-label");

  function syncTypeFields() {
    if (!typeSelect) return;
    var option = typeSelect.options[typeSelect.selectedIndex];
    var remote = option.dataset.remote === "1";
    var auth = option.dataset.auth === "1";
    var isSqlite = typeSelect.value === "sqlite";

    document.querySelectorAll("[data-remote-field]").forEach(function (el) {
      el.hidden = !remote;
    });
    document.querySelectorAll("[data-auth-field]").forEach(function (el) {
      el.hidden = !auth;
    });
    if (databaseLabel) {
      databaseLabel.textContent = isSqlite ? "SQLite 文件绝对路径 *" : "数据库名 *";
    }
    if (portInput) {
      portInput.placeholder = option.dataset.defaultPort || "";
    }
  }

  if (typeSelect) {
    typeSelect.addEventListener("change", function () {
      var option = typeSelect.options[typeSelect.selectedIndex];
      if (portInput && !portInput.value && option.dataset.remote === "1") {
        portInput.value = option.dataset.defaultPort || "";
      }
      syncTypeFields();
    });
    syncTypeFields();
  }

  document.querySelectorAll("[data-test-url]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var target = document.querySelector(button.dataset.target);
      if (!target) return;
      button.disabled = true;
      target.className = "test-result";
      target.textContent = "测试中…";
      try {
        var response = await fetch(button.dataset.testUrl, { method: "POST" });
        var data = await response.json();
        var suffix = data.server_version ? "（" + data.server_version + "）" : "";
        var latency = data.latency_ms != null ? " · " + data.latency_ms + "ms" : "";
        target.textContent = (data.ok ? "✓ " : "✗ ") + (data.message || "未知结果") + suffix + latency;
        target.classList.add(data.ok ? "ok" : "error");
      } catch (error) {
        target.textContent = "✗ 请求失败：" + error;
        target.classList.add("error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var source = document.querySelector(button.dataset.copy);
      if (!source) return;
      var text = source.textContent.trim();
      var original = button.textContent;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          var textarea = document.createElement("textarea");
          textarea.value = text;
          textarea.style.position = "fixed";
          textarea.style.opacity = "0";
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          document.body.removeChild(textarea);
        }
        button.textContent = "已复制";
      } catch (error) {
        button.textContent = "复制失败";
      }
      setTimeout(function () {
        button.textContent = original;
      }, 1600);
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    });
  });
})();
