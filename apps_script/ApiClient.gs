/** Thin HTTP client for the backend manager_api. Config lives in Script Properties. */

function _props() {
  return PropertiesService.getScriptProperties();
}

function _apiBase() {
  var url = _props().getProperty('API_BASE_URL');
  if (!url) throw new Error('API_BASE_URL не задан. Запустите «Настройка / API-ключ».');
  return url.replace(/\/+$/, '');
}

function _apiKey() {
  var key = _props().getProperty('API_KEY');
  if (!key) throw new Error('API_KEY не задан. Запустите «Настройка / API-ключ».');
  return key;
}

function _request(method, path, body) {
  var options = {
    method: method,
    contentType: 'application/json',
    headers: {
      'X-API-Key': _apiKey(),
      'ngrok-skip-browser-warning': 'true'
    },
    muteHttpExceptions: true
  };
  if (body) options.payload = JSON.stringify(body);

  var resp = UrlFetchApp.fetch(_apiBase() + path, options);
  var code = resp.getResponseCode();
  var text = resp.getContentText();
  var json;
  try { json = JSON.parse(text); } catch (e) { json = { message: text }; }

  if (code >= 200 && code < 300) return json;
  throw new Error((json && json.message) || ('HTTP ' + code));
}

function apiCreateBooking(payload) { return _request('post',   '/api/manager/bookings', payload); }
function apiCancelBooking(id)      { return _request('delete', '/api/manager/bookings/' + id); }
function apiPatch(id, patch)       { return _request('patch',  '/api/manager/bookings/' + id, patch); }
function apiListBookings(from, to) { return _request('get', '/api/manager/bookings?from=' + from + '&to=' + to);}
function apiDailyRefresh() {return _request('post', '/api/manager/bookings/daily_refresh');}
