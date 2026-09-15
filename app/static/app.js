// All page JavaScript lives here so the Content-Security-Policy can forbid inline scripts.
(function () {
  "use strict";

  // Language picker: submit on change
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });

  // Admin form editor: delete row / add row
  document.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-delete-row]");
    if (button) {
      button.closest("tr").remove();
    }
  });

  // Forms with data-confirm ask before submitting (delete a vehicle, re-evaluate)
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) { event.preventDefault(); }
    });
  });

  var addRow = document.getElementById("addrow");
  if (addRow) {
    addRow.addEventListener("click", function () {
      var tbody = document.querySelector("#modtable tbody");
      var idx = Date.now() % 1000000; // unique index; the server does not care about numeric order
      var profiles = JSON.parse(addRow.dataset.profiles);
      var tr = document.createElement("tr");
      tr.innerHTML =
        '<td><input class="mono" type="text" name="mod-' + idx + '-id"></td>' +
        '<td><input type="text" name="mod-' + idx + '-label"></td>' +
        '<td><input class="mono" type="text" name="mod-' + idx + '-match"></td>' +
        '<td><input class="mono" type="text" name="mod-' + idx + '-extract" placeholder="(last number in the string)"></td>' +
        profiles.map(function (p) {
          return '<td class="num"><input type="number" name="mod-' + idx + '-level-' + p + '"></td>';
        }).join("") +
        '<td class="crit"><input type="checkbox" name="mod-' + idx + '-critical" value="yes" checked></td>' +
        '<td class="del"><button type="button" class="small danger" data-delete-row>Delete</button></td>';
      tbody.appendChild(tr);
    });
  }

  // --- Passkeys (WebAuthn) ---------------------------------------------------
  function b64uToBuf(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) { s += "="; }
    var bin = window.atob(s), buf = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) { buf[i] = bin.charCodeAt(i); }
    return buf.buffer;
  }
  function bufToB64u(buf) {
    var bytes = new Uint8Array(buf), s = "";
    for (var i = 0; i < bytes.length; i++) { s += String.fromCharCode(bytes[i]); }
    return window.btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function showPasskeyError(message) {
    var box = document.getElementById("passkey-error");
    if (box) { box.textContent = message; box.hidden = false; }
  }
  function serializeCredential(cred) {
    var r = cred.response, out = {
      id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
      clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {},
      authenticatorAttachment: cred.authenticatorAttachment || null,
      response: { clientDataJSON: bufToB64u(r.clientDataJSON) }
    };
    if (r.attestationObject) {
      out.response.attestationObject = bufToB64u(r.attestationObject);
      if (r.getTransports) { out.response.transports = r.getTransports(); }
    } else {
      out.response.authenticatorData = bufToB64u(r.authenticatorData);
      out.response.signature = bufToB64u(r.signature);
      out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null;
    }
    return out;
  }
  async function postJson(url, body, csrf) {
    var headers = { "Content-Type": "application/json" };
    if (csrf) { headers["X-CSRF-Token"] = csrf; }
    var response = await fetch(url, { method: "POST", headers: headers, body: JSON.stringify(body || {}), credentials: "same-origin" });
    var data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok) { throw new Error((data && data.detail) || ("Request failed (" + response.status + ")")); }
    return data;
  }
  function creationOptions(options) {
    options.challenge = b64uToBuf(options.challenge);
    options.user.id = b64uToBuf(options.user.id);
    (options.excludeCredentials || []).forEach(function (c) { c.id = b64uToBuf(c.id); });
    return options;
  }
  function requestOptions(options) {
    options.challenge = b64uToBuf(options.challenge);
    (options.allowCredentials || []).forEach(function (c) { c.id = b64uToBuf(c.id); });
    return options;
  }
  var supported = !!(window.PublicKeyCredential && navigator.credentials);

  var registerButton = document.getElementById("passkey-register");
  if (registerButton) {
    registerButton.addEventListener("click", async function () {
      if (!supported) { return showPasskeyError("This browser does not support passkeys."); }
      try {
        var options = await postJson("/admin/profile/passkey/options", {}, registerButton.dataset.csrf);
        var cred = await navigator.credentials.create({ publicKey: creationOptions(options) });
        var nameInput = document.getElementById("passkey-name");
        await postJson("/admin/profile/passkey/register", { credential: serializeCredential(cred), name: nameInput ? nameInput.value : "" }, registerButton.dataset.csrf);
        window.location.href = "/admin/profile";
      } catch (e) { showPasskeyError(e.message || String(e)); }
    });
  }

  async function passkeyLogin(button) {
    if (!supported) { return showPasskeyError("This browser does not support passkeys."); }
    try {
      var options = await postJson("/admin/login/passkey/options", {});
      var cred = await navigator.credentials.get({ publicKey: requestOptions(options) });
      var result = await postJson("/admin/login/passkey/verify", { credential: serializeCredential(cred), next: button.dataset.next || "/admin" });
      window.location.href = result.next || "/admin";
    } catch (e) { showPasskeyError(e.message || String(e)); }
  }
  ["passkey-login", "passkey-signin"].forEach(function (id) {
    var button = document.getElementById(id);
    if (button) { button.addEventListener("click", function () { passkeyLogin(button); }); }
  });
})();
