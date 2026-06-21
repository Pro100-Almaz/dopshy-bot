/** New-booking sidebar. */

function showNewBookingSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('sidebar')
    .setTitle('Новая бронь');
  SpreadsheetApp.getUi().showSidebar(html);
}

/** Field options shown in the sidebar dropdown. Keep in sync with BOOKING_FIELDS. */
function getFieldOptions() {
  return [
    { id: 1, label: 'Поле 1 (5x5)' },
    { id: 2, label: 'Поле 2 (6x6)' },
    { id: 3, label: 'Поле 3 (5x5)' }
  ];
}

/**
 * Called from the sidebar via google.script.run. Creates a booking on the
 * backend and appends the returned row to the sheet.
 * Returns {ok, message} so the sidebar can show errors without closing.
 */
function submitNewBooking(form) {
  try {
    var payload = {
      field: Number(form.field),
      date: form.date,                 // YYYY-MM-DD
      time_start: form.start,          // HH:MM
      time_end: form.end,              // HH:MM
      customer: form.customer || '',
      phone: form.phone || '',
      notes: form.notes || '',
      repeat: form.repeat || 'none',    // none, daily, weekly, monthly
      end_date: form.endDate,
      client_token: Utilities.getUuid(),
      reserved_until: form.reservedUntil,
      updated_by: user.getEmail(),
    };
    var res = apiCreateBooking(payload);
    if (!res.ok) return { ok: false, message: res.message || 'Ошибка' };

    refreshFromServer();

    var bookingId = res.data.booking_id;
    return { ok: true, message: 'Бронь создана (#' + bookingId + ')' };

  } catch (err) {
    return { ok: false, message: err.message };
  }
}
