
/** New-grouping sidebar. */

function showNewGroupingSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('group_sidebar')
    .setTitle('Новая группа');
  SpreadsheetApp.getUi().showSidebar(html);
}


/**
 * Called from the sidebar via google.script.run. Creates a booking on the
 * backend and appends the returned row to the sheet.
 * Returns {ok, message} so the sidebar can show errors without closing.
 */
function submitNewGrouping(form) {
  try {
    var payload = {
      group_type: form.group_type,
      group_name: form.group_name,
      training_day: form.training_day,                 // YYYY-MM-DD
      time_start: form.time_start,          // HH:MM
      time_end: form.time_end,              // HH:MM
      max_cap: form.max_cap
    };
    var res = apiCreateGrouping(payload);
    if (!res.ok) return { ok: false, message: res.message || 'Ошибка' };


    return { ok: true, message: 'Бронь создана (#' + payload.group_name + ')' };
  } catch (err) {
    return { ok: false, message: err.message };
  }
}
