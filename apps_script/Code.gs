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
  CUSTOMER: 6, NOTES: 7, STATUS: 8, LAST_SYNCED: 9
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Менеджер')
    .addItem('Новая бронь…', 'showNewBookingSidebar')
    .addItem('Отменить выбранную строку', 'cancelSelectedRow')
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
  var sheet = SpreadsheetApp.getActiveSpreadsheet();
  var col = Number(sheet.getActiveRange().getColumn());
  if (col !== COL.CUSTOMER && col !== COL.NOTES) return;
  var row = Number(sheet.getActiveRange().getRow());
  if (row === 1) return; // header

  var bookingId = sheet.getActiveSheet().getRange(row, COL.BOOKING_ID).getValue();
  if (!bookingId) return; // unsynced row being typed manually

  var field = col === COL.CUSTOMER ? 'customer' : 'notes';
  var patch = {};
  patch[field] = sheet.getActiveSheet().getRange(row, col).getValue();
  try {
    apiPatch(bookingId, patch);
    sheet.toast('Обновлено: ' + field, 'Менеджер', 3);
  } catch (err) {
    SpreadsheetApp.getUi().alert('Не удалось обновить: ' + err.message);
  }
  refreshFromServer();
  apiDailyRefresh();
}
