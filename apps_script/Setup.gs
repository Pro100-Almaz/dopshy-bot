/** One-time setup dialog: store backend URL + API key in Script Properties. */

function showSetupDialog() {
  var ui = SpreadsheetApp.getUi();

  var url = ui.prompt('Базовый URL API', 'напр. https://api.yourdomain.kz', ui.ButtonSet.OK_CANCEL);
  if (url.getSelectedButton() !== ui.Button.OK) return;

  var key = ui.prompt('API-ключ', '', ui.ButtonSet.OK_CANCEL);
  if (key.getSelectedButton() !== ui.Button.OK) return;

  _props().setProperties({
    API_BASE_URL: url.getResponseText().trim(),
    API_KEY: key.getResponseText().trim(),
    BOOKINGS_SHEET_NAME: 'Bookings',
    WEEK_SHEET_NAMES: ["This Week 1", "This Week 2", "This Week 3"],
    ACADEM_GROUP_SHEETS: ["Boxing_Groups", "Football_Groups"],
    ACADEM_TRIAL_SHETS: ["Boxing_Trials", "Football_Trials"],
    TZ_OFFSET: '+05:00'
  });
  ui.alert('Сохранено.');
}
