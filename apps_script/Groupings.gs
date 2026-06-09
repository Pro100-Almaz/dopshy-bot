
function deactivateGroupSelected() {
  var ui = SpreadsheetApp.getUi();
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = spreadsheet.getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  if (row === 1) { spreadsheet.toast('Сначала выберите строку с записью'); return; }

  var group_id = sheet.getRange(row, GROUP_COL.GROUP_ID).getValue();
  if (!group_id) { spreadsheet.toast('В этой строке нет group_id'); return; }

  var resp = ui.alert('Данная группа #' + group_id + ' будет удалена.', ui.ButtonSet.YES_NO);
  if (resp !== ui.Button.YES) return;

  try {
    apiDeactivateGrouping(group_id);

    spreadsheet.toast('Изменено');

  } catch (err) {
    ui.alert('Не удалось изменить: ' + err.message);
  }
}