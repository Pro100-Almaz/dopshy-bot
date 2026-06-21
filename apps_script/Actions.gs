/** Menu actions: cancel a row, refresh the sheet from the backend. */

function _bookingsSheet() {
  var name = _props().getProperty('BOOKINGS_SHEET_NAME') || 'Bookings';
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sheet) throw new Error('Лист «' + name + '» не найден.');
  return sheet;
}

function cancelSelectedRow() {
  var ui = SpreadsheetApp.getUi();
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet()
  var sheet = spreadsheet.getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  if (row === 1) { spreadsheet.toast('Сначала выберите строку с бронью'); return; }

  var bookingId = sheet.getRange(row, COL.BOOKING_ID).getValue();
  if (!bookingId) { spreadsheet.toast('В этой строке нет booking_id'); return; }

  var resp = ui.alert('Отменить бронь?', 'Бронь #' + bookingId + ' будет отменена.', ui.ButtonSet.YES_NO);
  if (resp !== ui.Button.YES) {return;}

  try {
    apiCancelBooking(bookingId);
    spreadsheet.toast('Отменено');
  } catch (err) {
    ui.alert('Не удалось отменить: ' + err.message);
  }
  refreshFromServer();
}

function cancelRepetitiveBooking() {
  var ui = SpreadsheetApp.getUi();
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet()
  var sheet = spreadsheet.getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  if (row === 1) { spreadsheet.toast('Сначала выберите строку с бронью'); return; }

  var bookingId = sheet.getRange(row, COL.BOOKING_ID).getValue();
  if (!bookingId) { spreadsheet.toast('В этой строке нет booking_id'); return; }

  var resp = ui.alert('Отменить бронь?', 'Бронь #' + bookingId + ' и последующие будут отменены.', ui.ButtonSet.YES_NO);
  if (resp !== ui.Button.YES) {return;}

  try {
    apiCancelRepetitiveBooking(bookingId);
    spreadsheet.toast('Отменено');
  } catch (err) {
    ui.alert('Не удалось отменить: ' + err.message);
  }
  refreshFromServer();
}

function getKeyByValue(obj, value) {
  return Object.keys(obj).find(key => obj[key] === value);
}

const STATUS_NAMES = {
  'awaiting_payment': 'ОЖИДАЕТ ОПЛАТЫ',
  'cancelled': 'ОТМЕНЕНО',
  'confirmed': 'ПОДТВЕРЖДЕНО',
  'failed': 'ПРОВАЛ',
  'draft': 'ЧЕРНОВИК',
  'unpaid': 'НЕ ОПЛАЧЕНО',
}

function showCancelDialog() {
  var ui = SpreadsheetApp.getUi();
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet()
  var sheet = spreadsheet.getActiveSheet();
  var row = sheet.getActiveRange().getRow();
  if (row === 1) { spreadsheet.toast('Сначала выберите строку с бронью'); return; }

  var bookingId = sheet.getRange(row, COL.BOOKING_ID).getValue();
  var status = getKeyByValue(STATUS_NAMES, sheet.getRange(row, COL.STATUS).getValue());
  if (!bookingId) { spreadsheet.toast('В этой строке нет номера брони'); return; }
  if (!status) { spreadsheet.toast('В этой строке неправильный статус'); return; }


  var html = HtmlService.createTemplateFromFile('statusModal');

  html.bookingId = bookingId;
  html.status = String(status);

  SpreadsheetApp.getUi().showModalDialog(
    html.evaluate().setWidth(300).setHeight(200),
    'Изменить статус брони'
  );
}

function changeStatusSelected(bookingId, status) {
  var ui = SpreadsheetApp.getUi();
  var spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = spreadsheet.getActiveSheet();

  var resp = ui.alert(
    'Статус брони #' + bookingId + ' будет изменена на ' + STATUS_NAMES[status] + '.',
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  var patch = {"status": status, "source": user.getEmail()}
  try {
    apiPatch(bookingId, patch);

    var data = sheet.getDataRange().getValues();
    for (var i = 1; i < data.length; i++) {
      if (data[i][COL.BOOKING_ID - 1] == bookingId) {
        sheet.getRange(i + 1, COL.STATUS).setValue(STATUS_NAMES[status]);
        break;
      }
    }
    spreadsheet.toast('Изменено');

  } catch (err) {
    ui.alert('Не удалось изменить: ' + err.message);
  }
  refreshFromServer();
}

function formatDate(dateStr) {
  if (!dateStr) return "";
  var tz = Session.getScriptTimeZone();
  return Utilities.formatDate(new Date(dateStr), tz, 'yyyy-MM-dd HH:mm');
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

  var header = ['Номер брони', 'Поле', 'Дата', 'Начало', 'Конец',
                'Имя клиента', 'Телефон', 'Заметки', 'Статус', 'Посл. обновление',
                'Менеджер', 'Резерв до', 'Сумма', 'Оплачено', 'Остаток суммы',
                'Дата чека', 'Kaspi QR', 'Наличные'];
  var rows = [header];
  var nowStr = Utilities.formatDate(new Date(), tz, 'yyyy-MM-dd HH:mm');
  res.data.forEach(function (b) {
    var price = Number(b.price_total) || 0;
    var paid = Number(b.payment_current) || 0;
    rows.push([
      b.id, b.field, b.date,
      String(b.time_start).slice(0, 5), String(b.time_end).slice(0, 5),
      b.customer_name || '', b.phone, b.notes || '', String(STATUS_NAMES[b.state]), nowStr,
      b.source, formatDate(b.reserved_until), price, paid, Math.max(0, price - paid),
      formatDate(b.last_receipt_date), Number(b.paid_kaspi_qr) || 0, Number(b.paid_cash) || 0
    ]);
  });

  sheet.clearContents();
  sheet.getRange(1, 1, rows.length, header.length).setValues(rows);
  SpreadsheetApp.getActiveSpreadsheet().toast('Обновлено: ' + (rows.length - 1) + ' броней');
}



