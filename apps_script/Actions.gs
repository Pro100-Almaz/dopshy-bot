/** Menu actions: cancel a row, refresh the sheet from the backend. */

function _bookingsSheet() {
  var name = _props().getProperty('BOOKINGS_SHEET_NAME') || 'Bookings';
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sheet) throw new Error('Лист «' + name + '» не найден.');
  return sheet;
}

function cancelSelectedRow() {
  var ui = SpreadsheetApp.getUi();
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  if (row === 1) { sheet.toast('Сначала выберите строку с бронью'); return; }

  var bookingId = sheet.getRange(row, COL.BOOKING_ID).getValue();
  if (!bookingId) { sheet.toast('В этой строке нет booking_id'); return; }

  var resp = ui.alert('Отменить бронь?', 'Бронь #' + bookingId + ' будет отменена.',
                      ui.ButtonSet.YES_NO);
  if (resp !== ui.Button.YES) return;

  try {
    apiCancelBooking(bookingId);
    sheet.getRange(row, COL.STATUS).setValue('CANCELLED');
    sheet.toast('Отменено');
  } catch (err) {
    ui.alert('Не удалось отменить: ' + err.message);
  }
}

/** Overwrite the sheet with the backend's current view for the next ~60 days. */
function refreshFromServer() {
  var sheet = _bookingsSheet();
  var tz = Session.getScriptTimeZone();
  var today = new Date();
  var to = new Date(today.getTime() + 60 * 24 * 3600 * 1000);
  var from = Utilities.formatDate(today, tz, 'yyyy-MM-dd');
  var toStr = Utilities.formatDate(to, tz, 'yyyy-MM-dd');

  var res = apiListBookings(from, toStr);
  if (!res.ok) { SpreadsheetApp.getUi().alert('Ошибка: ' + res.message); return; }

  var header = ['booking_id', 'field', 'date', 'start', 'end',
                'customer', 'notes', 'status', 'last_synced'];
  var rows = [header];
  var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm');
  res.data.forEach(function (b) {
    rows.push([
      b.id, b.field, b.date,
      String(b.time_start).slice(0, 5), String(b.time_end).slice(0, 5),
      b.customer_name || '', b.notes || '', String(b.state).toUpperCase(), nowStr
    ]);
  });

  sheet.clearContents();
  sheet.getRange(1, 1, rows.length, header.length).setValues(rows);
  sheet.toast('Обновлено: ' + (rows.length - 1) + ' броней');
}
