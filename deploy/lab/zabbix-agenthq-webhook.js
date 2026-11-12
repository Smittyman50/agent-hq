// Zabbix media type script: sends problems and recoveries to Agent HQ's webhook.
// Alerts -> Media types -> Create media type -> Type: Webhook, paste this as the Script,
// and add these parameters (docs/lab-alerts.md):
//   url       https://shockwave.home.arpa/hooks/zabbix
//   token     the hook's secret (from Settings -> Events -> Webhooks)
//   domain    {EVENT.TAGS.domain}     tag hosts or templates domain=network / domain=systems
//   event_id  {EVENT.ID}
//   status    {EVENT.STATUS}          PROBLEM or RESOLVED
//   severity  {EVENT.SEVERITY}
//   host      {HOST.NAME}
//   problem   {EVENT.NAME}
//   opdata    {EVENT.OPDATA}
//   tags      {EVENT.TAGS}
var p = JSON.parse(value);

function clean(v) {
    // Zabbix leaves macros it can't resolve as text, e.g. "{EVENT.TAGS.domain}".
    return (typeof v === 'string' && v.indexOf('{') === 0) ? '' : (v || '');
}

var domain = clean(p.domain).toLowerCase();
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('X-Webhook-Token: ' + p.token);
req.addHeader('X-Event: ' + (domain === 'network' || domain === 'systems' ? domain : 'event'));
req.addHeader('X-Request-Id: zabbix-' + p.event_id + '-' + clean(p.status));

var body = JSON.stringify({
    event_id: p.event_id, status: clean(p.status), severity: clean(p.severity),
    host: clean(p.host), problem: clean(p.problem), opdata: clean(p.opdata), tags: clean(p.tags)
});
var resp = req.post(p.url, body);
var code = req.getStatus();
if (code < 200 || code >= 300) {
    throw 'Agent HQ answered ' + code + ': ' + resp;
}
return 'OK';
