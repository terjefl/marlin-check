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
})();
