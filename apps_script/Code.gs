/**
 * Container-bound Apps Script for the Dopshy "Bookings" spreadsheet.
 *
 * The backend PostgreSQL DB is the source of truth. Manager actions here call
 * the backend manager_api; the sheet is a synced view.
 *
 * Sheet columns (row 1 = header):
 *   A booking_id | B field | C date | D start | E end |
 *   F customer   | G notes | H status | I last_synced
 *
 * Managers may free-edit F (customer) and G (notes); structural columns are
 * changed only through the menu/sidebar. Protect A,B,C,D,E,H,I via sheet
 * protection so the onEdit handler only ever fires for F/G.
 */

var COL = {
  BOOKING_ID: 1, FIELD: 2, DATE: 3, START: 4, END: 5,
  CUSTOMER: 6, PHONE: 7, NOTES: 8, STATUS: 9, LAST_SYNCED: 10, UPDATED_BY: 11, RESERVED_UNTIL: 12,
  PRICE_TOTAL: 13, PAYMENT_CURRENT: 14, REMAINDER: 15, RECEIPT_DATE: 16, KASPI_QR: 17, CASH: 18
};
var GROUP_COL = {
  GROUP_ID: 1, GROUP_NAME: 2, MAX_CAP : 3, CURR_CAP: 4,
  TRAINGING_DAY: 5, START_TIME: 6, END_TIME: 7
}
const user = Session.getActiveUser();

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Менеджер')
    .addItem('Новая бронь…', 'showNewBookingSidebar')
    .addItem('Изменить статус выбранной строки', 'showCancelDialog')
    // .addItem('Отменить выбранную строку', 'cancelSelectedRow')
    .addItem('Отменить последующие брони этой группы', 'cancelRepetitiveBooking')
    .addSeparator()
    .addItem('Создать группу', 'showNewGroupingSidebar')
    .addItem('Деактивировать группу', "deactivateGroupSelected")
    .addSeparator()
    .addItem('Обновить с сервера', 'refreshFromServer')
    .addSeparator()
    .addItem('Настройка / API-ключ', 'showSetupDialog')
    .addToUi();
}

/**
 * Free-edit columns (customer, notes) are PATCHed to the backend. On failure
 * the cell is reverted to its previous value.
 */
function onEditManual() {
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = spreadsheet.getActiveSheet();
  var sheetName = sheet.getName();

  var range = sheet.getActiveRange();
  var col = Number(range.getColumn());
  var row = Number(range.getRow());

  if (row === 1) return; // header

  var groupSheets = ['Boxing_Groups', 'Football_Groups'];


  if (!groupSheets.includes(sheetName)){

    var editableCols = [COL.CUSTOMER, COL.NOTES, COL.KASPI_QR, COL.CASH];
    if (!editableCols.includes(col)) return;

    var bookingId = sheet.getRange(row, COL.BOOKING_ID).getValue();
    if (!bookingId) return; // unsynced row being typed manually

    var colFieldMap = {};
    colFieldMap[COL.CUSTOMER] = 'customer';
    colFieldMap[COL.NOTES] = 'notes';
    colFieldMap[COL.KASPI_QR] = 'paid_kaspi_qr';
    colFieldMap[COL.CASH] = 'paid_cash';
    var field = colFieldMap[col];

    var patch = {};
    patch[field] = sheet.getRange(row, col).getValue();
    patch["source"] = user.getEmail();

    try {
      apiPatch(bookingId, patch);
      spreadsheet.toast('Обновлено: ' + field, 'Менеджер', 3);
    } catch (err) {
      SpreadsheetApp.getUi().alert('Не удалось обновить: ' + err.message);
    }
    refreshFromServer();

  }else{

    var allowedGroupCols = [
      GROUP_COL.GROUP_NAME,
      GROUP_COL.MAX_CAP
    ];

    if (!allowedGroupCols.includes(col)) return;

    var groupId = sheet.getRange(row, GROUP_COL.GROUP_ID).getValue();
    if (!groupId){return};


    var field = col;

    if (col === GROUP_COL.GROUP_NAME) {
      field = 'group_name';
    } else if (col === GROUP_COL.MAX_CAP) {
      var newMaxCap = sheet.getRange(row, GROUP_COL.MAX_CAP).getValue();
      var currCap = sheet.getRange(row, GROUP_COL.CURR_CAP).getValue();

      if (newMaxCap < currCap) {
        SpreadsheetApp.getUi().alert(
          'Максимальная вместимость не может быть меньше текущего количества учеников.\n\n' +
          'Текущая вместимость: ' + currCap + '\n' +
          'Введённая максимальная вместимость: ' + newMaxCap
        );

        apiRefreshGroupTables();
        return;
      } else{
        field = 'max_cap';
      }
    }

    var patch = {};
    patch[field] = sheet.getRange(row, col).getValue();
    try {
      apiPatchGrouping(groupId, patch);
      spreadsheet.toast('Обновлено: ' + field, 'Менеджер', 3);
    } catch (err) {
      SpreadsheetApp.getUi().alert('Не удалось обновить: ' + err.message);
      apiRefreshGroupTables();
    }
  }
}
