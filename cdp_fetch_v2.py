import subprocess, time, socket, json, urllib.request, websocket, re, os

def fetch_with_cdp(url, timeout=30):
    """Fetch a URL using headless Chromium via CDP. Returns rendered text."""
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    
    proc = subprocess.Popen([
        '/snap/bin/chromium', '--headless=new', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--no-first-run', '--remote-allow-origins=*',
        '--remote-debugging-port=' + str(port), '--user-data-dir=/tmp/cdp-fetch',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        for i in range(30):
            try:
                r = urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/json/version', timeout=1)
                break
            except: time.sleep(0.5)
        else:
            return None
        
        r = urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/json')
        pages = json.loads(r.read())
        page_ws = next(p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page')
        ws = websocket.create_connection(page_ws, timeout=30, origin='*')
        
        mid = 0
        def cdp(method, params=None):
            nonlocal mid; mid += 1
            msg = {'id': mid, 'method': method}
            if params: msg['params'] = params
            ws.send(json.dumps(msg))
            deadline = time.time() + 30
            while time.time() < deadline:
                data = json.loads(ws.recv())
                if data.get('id') == mid: return data
            return {}
        
        cdp('Page.enable')
        cdp('Runtime.enable')
        cdp('Page.navigate', {'url': url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(ws.recv())
            if data.get('method') == 'Page.loadEventFired': break
        
        time.sleep(2)
        # Accept cookies if present
        cdp('Runtime.evaluate', {'expression': "var b=document.querySelector('#sp-cc-accept');if(b)b.click()", 'returnByValue': True})
        time.sleep(2)
        # Scroll to trigger lazy load
        cdp('Runtime.evaluate', {'expression': 'window.scrollTo(0,600)', 'returnByValue': True})
        time.sleep(2)
        
        # Extract clean text
        r = cdp('Runtime.evaluate', {
            'expression': '(function(){var c=document.body.cloneNode(true);c.querySelectorAll("script,style,noscript,svg,[aria-hidden=true],[hidden]").forEach(function(e){e.remove()});var t=(c.innerText||c.textContent||"").replace(/\n{3,}/g,"\n\n").trim();return(document.title?document.title+"\n\n":"")+t;})()',
            'returnByValue': True,
        })
        text = r.get('result', {}).get('result', {}).get('value', '')
        ws.close()
        return text if text else None
    finally:
        proc.terminate()
        proc.wait()

if __name__ == '__main__':
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://example.com'
    text = fetch_with_cdp(url)
    if text:
        print(text[:3000])
        print('\n--- %d chars total ---' % len(text))
    else:
        print('Failed to fetch')
