"""Explicit real-Zen acceptance: existing Stremio tab and YouTube media, three runs.

Requires the active installed companion and registered host. Never installs an extension,
creates a profile, navigates to a different site, or injects physical input.
Use only when the always-ready companion is installed and Zen is unfocused.
"""
from pathlib import Path
import argparse
import importlib.util
import json
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from integration_test import JsonProcess


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Perform the authorized live browser actions')
    parser.add_argument('--hwnd', type=int, required=True)
    parser.add_argument('--youtube-tab', type=int)
    parser.add_argument('--stremio-tab', type=int)
    args = parser.parse_args()
    require(args.run, 'Supply --run explicitly when the installed companion is ready')
    spec = importlib.util.spec_from_file_location('bwc_live_metadata', ROOT / 'tests/native-input/acceptance.py')
    metadata = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(metadata)
    monitor = metadata.Monitor()
    server = JsonProcess([sys.executable, '-u', str(ROOT / 'scripts/mcp_server.py')])
    report = {'passed': False, 'realBrowser': True, 'runs': [], 'fullSystemCoverage': False,
              'scope': 'Real tab selection and semantic media state; not pointer/keyboard, 60-second co-use or restart persistence'}
    monitoring = False
    try:
        server.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                 'clientInfo': {'name': 'zen-real-acceptance', 'version': '1'}})
        server.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        windows = server.tool('list_windows')[0]['windows']
        selected = [w for w in windows if w['hwnd'] == args.hwnd and w['process'].lower() == 'zen'
                    and w['selectable'] and not w['minimized']]
        require(len(selected) == 1, 'Selected existing Zen window is unavailable')
        target_pid = selected[0]['pid']
        server.tool('attach_window', {'hwnd': args.hwnd})

        def browser(op, values=None):
            response = server.tool('browser', {'operation': op, 'arguments': values or {}})[0]
            require(response.get('ok') is True, response.get('error', 'Browser operation failed'))
            return response.get('result', response)

        connection_started = time.monotonic()
        connection = browser('connect')
        window_seconds = connection.get('connectionWindowSeconds', 5)
        print('Connecting to the ready companion automatically; no toolbar click is needed.', flush=True)
        limit = time.monotonic() + window_seconds
        while not connection.get('bound'):
            response = server.rpc('tools/call', {'name': 'browser', 'arguments': {'operation': 'pair', 'arguments': {}}})
            payload = response.get('result', {})
            content = payload.get('structuredContent', {})
            if not payload.get('isError') and content.get('bound') is True:
                break
            report['lastPairingResponse'] = payload
            require(not payload.get('isError') and content.get('pending') is True,
                    'Automatic connection failed; no browser actions performed')
            require(time.monotonic() < limit, 'Automatic connection timed out; no browser actions performed')
            time.sleep(.25)
        report['automaticConnectionMilliseconds'] = round((time.monotonic()-connection_started)*1000)
        print('Connected. The live test starts after Zen stays in the background for two seconds.', flush=True)
        limit = time.monotonic() + 60
        stable_since = None
        while time.monotonic() < limit:
            foreground = metadata.U.GetForegroundWindow()
            if metadata.owner(foreground) != target_pid:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    break
            else:
                stable_since = None
            time.sleep(.05)
        else:
            raise RuntimeError('Zen remained foreground; no browser actions performed')
        monitoring = True
        monitor.start()
        monitor.phase = 'real_zen_actions'

        def isolated():
            require(monitor.failure is None, 'Focus monitor failed')
            require(metadata.owner(metadata.U.GetForegroundWindow()) != target_pid, 'Zen acquired foreground; stopping')
            require(not any(e['pid'] == target_pid and e['event'] in (3, 8) for e in monitor.events),
                    'Zen foreground/capture event observed; source cannot be attributed')
            require(not any(s['foregroundPid'] == target_pid or s['focusPid'] == target_pid for s in monitor.samples),
                    'Zen foreground/focus sample observed; stopping')

        listing = browser('observe', {'includePage': False})

        def choose(hosts, explicit, video=False):
            candidates = [t for t in listing['tabs'] if urlsplit(t['url']).hostname in hosts
                          and (explicit is None or t['id'] == explicit)
                          and (not video or urlsplit(t['url']).path == '/watch')]
            require(bool(candidates), 'No matching existing test tab is available')
            # Multiple existing tabs are legitimate. Test the active eligible
            # tab, otherwise the oldest eligible tab, and record exact IDs.
            chosen = sorted(candidates, key=lambda t: (not t['active'], t['id']))[0]
            report.setdefault('tabSelection', []).append({
                'candidateIds': [t['id'] for t in candidates], 'chosenId': chosen['id'],
                'title': chosen['title'], 'url': chosen['url'],
                'rule': 'explicit ID' if explicit is not None else 'active eligible tab, otherwise lowest ID'})
            return chosen['id']

        stremio = choose({'web.stremio.com'}, args.stremio_tab)
        youtube = choose({'www.youtube.com', 'youtube.com'}, args.youtube_tab, video=True)
        report['target'] = {'hwnd': args.hwnd, 'pid': target_pid, 'stremioTab': stremio, 'youtubeTab': youtube}

        def action(observation, action, **values):
            isolated()
            submitted = browser('input', {'observationId': observation['observationId'], 'action': action, **values})
            result = browser('verify', {'actionId': submitted['actionId']})
            isolated()
            require(result.get('effectVerified') is True, 'Action delivered but effect not verified: ' + action)
            return result

        def select(tab):
            observation = browser('observe', {'includePage': False})
            return action(observation, 'select_tab', tabId=tab)

        def media(action_name):
            observation = browser('observe', {'tabId': youtube, 'includePage': True, 'maxElements': 200})
            videos = [e for e in observation['page']['elements'] if e.get('role') == 'video'
                      and action_name in e.get('actions', [])]
            require(len(videos) == 1, 'YouTube did not expose exactly one eligible video')
            return action(observation, action_name, tabId=youtube, elementId=videos[0]['id'])

        for number in range(1, 4):
            select(stremio)
            select(youtube)
            # Prove an actual playing-to-paused transition instead of repeatedly
            # checking a video that was already paused before the test.
            played = media('media_play')
            paused = media('media_pause')
            report['runs'].append({'run': number, 'stremioSelected': True, 'youtubeSelected': True,
                                   'playingVerified': played.get('state', {}).get('paused') is False,
                                   'pausedVerified': paused.get('state', {}).get('paused') is True})
            require(report['runs'][-1]['playingVerified'] and report['runs'][-1]['pausedVerified'],
                    'Media verification did not establish both states')
            print(json.dumps(report['runs'][-1]), flush=True)
        isolated()
        require(bool(monitor.samples) and all(s['guiQuery'] and s['cursorQuery'] for s in monitor.samples),
                'Complete OS focus/cursor metadata was not available')
        report['passed'] = True
        report['finalBrowserState'] = 'YouTube selected and paused'
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        if monitoring:
            try:
                monitor.stop()
            except Exception as exc:
                report['passed'] = False
                report['monitorError'] = str(exc)
            report['metadataSamples'] = len(monitor.samples)
            report['targetForegroundOrCaptureEvents'] = sum(e['pid'] == target_pid and e['event'] in (3, 8) for e in monitor.events)
            if monitor.failure:
                report['passed'] = False
                report['monitorError'] = monitor.failure
        try:
            report['stop'] = server.tool('stop')[0]
        except Exception as exc:
            report['passed'] = False
            report['stopError'] = str(exc)
        try:
            server.close()
        except Exception as exc:
            report['passed'] = False
            report['clientCleanupError'] = str(exc)
        output = ROOT / 'tests/results/zen-live.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(report, indent=2) + '\n'
        output.write_text(serialized, encoding='utf-8')
        # Keep failed attempts as evidence when a later run succeeds.
        history = output.parent / 'zen-live-runs'
        history.mkdir(exist_ok=True)
        (history / f'{time.time_ns()}.json').write_text(serialized, encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
