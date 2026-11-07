// CreditsTracker — GNOME Shell top-bar indicator (GNOME 45+ / Ubuntu 24.04).
// All data comes from the shared core: `python3 ctrack.py status --json`.
// Loaded by extension.js from a fresh copy on every enable, so updates apply without logging out.

import GObject from 'gi://GObject';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';

import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const POLL_SECONDS = 60;      // the core only hits the network when an account is due
const TICK_SECONDS = 20;      // countdown refresh while the menu is open
const BAR_WIDTH = 364;        // = .ct-card content width (stylesheet.css)

const VENDOR_GLYPH = {anthropic: '✦', openai: '◎', github: '</>', bedrock: '☁', command: '>_'};
const ACCOUNT_TYPES = [
    ['claude', 'Claude', 'Own Claude Code config dir (~/.claude-<name>); sign-in opens the browser.'],
    ['codex', 'Codex', 'Own CODEX_HOME (~/.codex-<name>); needs the codex CLI; sign-in opens the browser.'],
    ['copilot', 'Copilot', 'GitHub device code — use the account holding the Copilot seat. For GitHub Enterprise with data residency, set its host (e.g. mycorp.ghe.com).'],
    ['command', 'Command', 'Any command printing its usage as CreditsTracker JSON (README → Your own provider).'],
];

// ─── helpers ────────────────────────────────────────────────────────────────

function usageClass(pct) {
    if (pct === null || pct === undefined)
        return null;
    if (pct >= 85)
        return 'ct-red';
    return pct >= 60 ? 'ct-orange' : 'ct-green';
}

function countdown(target, now) {
    const secs = Math.floor((target - now) / 1000);
    if (secs <= 0)
        return 'now';
    const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    if (d > 0)
        return `${d}d ${h}h`;
    if (h > 0)
        return `${h}h ${String(m).padStart(2, '0')}m`;
    return `${Math.max(m, 1)}m`;
}

function clock(date, now) {
    const hm = date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    if (date.toDateString() === now.toDateString())
        return hm;
    if (date - now < 6 * 86400e3)
        return `${date.toLocaleDateString([], {weekday: 'short'})} ${hm}`;
    return date.toLocaleDateString([], {day: 'numeric', month: 'short'});
}

function label(text, styleClass, opts = {}) {
    const {wrap, ...props} = opts;
    const l = new St.Label({text: text ?? '', style_class: styleClass ?? '', y_align: Clutter.ActorAlign.CENTER, ...props});
    if (wrap) {
        l.clutter_text.line_wrap = true;
        l.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        l.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    } else {
        l.clutter_text.ellipsize = Pango.EllipsizeMode.END;
    }
    return l;
}

function iconButton(iconName, tooltip, onClick, extraClass = '') {
    const b = new St.Button({
        style_class: `ct-icon-btn ${extraClass}`, can_focus: true, accessible_name: tooltip,
        child: new St.Icon({icon_name: iconName}),
    });
    b.connect('clicked', () => onClick());
    return b;
}

function textButton(text, onClick, extraClass = '') {
    const b = new St.Button({style_class: `ct-btn ${extraClass}`, label: text, can_focus: true});
    b.connect('clicked', () => onClick());
    return b;
}

function openUri(uri) {
    try {
        Gio.AppInfo.launch_default_for_uri(uri, global.create_app_launch_context(0, -1));
    } catch (e) {
        logError(e, 'CreditsTracker: cannot open uri');
    }
}

function copyText(text) {
    St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
}

// ─── core runner ────────────────────────────────────────────────────────────

class Core {
    constructor(extPath) {
        const candidates = [
            GLib.build_filenamev([extPath, 'ctrack.py']),
            GLib.build_filenamev([GLib.get_user_data_dir(), 'creditstracker', 'ctrack.py']),
        ];
        this.script = candidates.find(p => GLib.file_test(p, GLib.FileTest.EXISTS)) ?? candidates[0];
        this.python = GLib.find_program_in_path('python3') ?? '/usr/bin/python3';
        this.cancellable = new Gio.Cancellable();
    }

    argv(args) {
        return [this.python, '-I', this.script, ...args];
    }

    run(args) {
        return new Promise((resolve, reject) => {
            let proc;
            try {
                proc = Gio.Subprocess.new(this.argv(args),
                    Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            } catch (e) {
                reject(e);
                return;
            }
            proc.communicate_utf8_async(null, this.cancellable, (p, res) => {
                try {
                    const [, stdout, stderr] = p.communicate_utf8_finish(res);
                    if (stdout && stdout.trim())
                        resolve(stdout);
                    else
                        reject(new Error((stderr || '').trim().split('\n').pop() || 'ctrack failed'));
                } catch (e) {
                    reject(e);
                }
            });
        });
    }

    async json(args) {
        return JSON.parse(await this.run(args));
    }

    // Streams `ctrack login <id> --json` events (one JSON object per line).
    login(id, onEvent, onExit) {
        const proc = Gio.Subprocess.new(this.argv(['login', id, '--json']),
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE);
        const stream = new Gio.DataInputStream({base_stream: proc.get_stdout_pipe(), close_base_stream: true});
        const readLine = () => {
            stream.read_line_async(GLib.PRIORITY_DEFAULT, this.cancellable, (s, res) => {
                let line;
                try {
                    [line] = s.read_line_finish_utf8(res);
                } catch (e) {
                    return;
                }
                if (line === null)
                    return;
                try {
                    onEvent(JSON.parse(line));
                } catch (e) { /* non-JSON noise */ }
                readLine();
            });
        };
        readLine();
        proc.wait_async(this.cancellable, (p, res) => {
            try {
                p.wait_finish(res);
            } catch (e) {
                return;
            }
            onExit(p.get_if_exited() ? p.get_exit_status() : -1);
        });
        return proc;
    }

    destroy() {
        this.cancellable.cancel();
    }
}

// ─── indicator ──────────────────────────────────────────────────────────────

// Each loaded copy registers its own GType: the shell keeps every type for the session.
const Indicator = GObject.registerClass({
    GTypeName: `CreditsTrackerIndicator_${GLib.uuid_string_random().replaceAll('-', '')}`,
}, class CreditsTrackerIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.5, 'CreditsTracker');
        this._core = new Core(ext.path);
        this._settings = ext.getSettings();
        this._status = null;
        this._error = null;
        this._loading = false;
        this._refreshing = false;
        this._pinging = new Set();
        this._pingMsgs = new Map();
        this._login = null;        // {id, label, code, url, message, finished, failed}
        this._loginProc = null;
        this._addForm = null;      // {type, label, command, host, busy, error}
        this._edit = null;         // {id, type, label, host, origHost}
        this._confirmRemove = null;
        this._lastLoaded = null;

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        box.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(GLib.build_filenamev([ext.path, 'icons', 'ct-gauge-symbolic.svg'])),
            style_class: 'system-status-icon',
        }));
        this._panelLabel = new St.Label({text: '', y_align: Clutter.ActorAlign.CENTER, style_class: 'ct-panel-label'});
        box.add_child(this._panelLabel);
        this.add_child(box);

        this.menu.box.add_style_class_name('ct-menu');
        this._section = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._section);
        this._buildSettings();
        this._settingsChangedId = this._settings.connect('changed', () => {
            this._pctSwitch.setToggleState(this._settings.get_boolean('show-percent'));
            this._update();
        });
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open) {
                this._render();
                this._load([]);
            } else {
                this._confirmRemove = null;
                this._edit = null;
            }
        });
        // PopupMenu.open() refuses to open an empty menu, so the content must exist before the
        // first click ('open-state-changed' never fires otherwise).
        this._render();

        this._load(['--cached']).then(() => this._load([]));
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, POLL_SECONDS, () => {
            this._load([], true);
            return GLib.SOURCE_CONTINUE;
        });
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            if (this.menu.isOpen && !this._addForm && !this._edit)
                this._render();
            return GLib.SOURCE_CONTINUE;
        });
    }

    // ── data ──

    async _load(extra, poll = false) {
        if (poll && this._loading)
            return;
        this._loading = true;
        try {
            this._status = await this._core.json(['status', '--json', '--compact', ...extra]);
            this._error = null;
            this._lastLoaded = new Date();
        } catch (e) {
            if (this._destroyed)
                return;
            this._error = e.message;
        } finally {
            this._loading = false;
        }
        if (!this._destroyed)
            this._update();
    }

    async _refreshAll() {
        if (this._refreshing)
            return;
        this._refreshing = true;
        this._update();
        await this._load(['--force']);
        this._refreshing = false;
        this._update();
    }

    async _refreshAccount(id) {
        try {
            await this._core.run(['status', '--json', '--compact', '--force', id]);
        } catch (e) { /* shown by the next load */ }
        await this._load(['--cached']);
    }

    async _ping(ids) {
        ids = ids.filter(id => !this._pinging.has(id));
        if (!ids.length)
            return;
        ids.forEach(id => {
            this._pinging.add(id);
            this._pingMsgs.delete(id);
        });
        this._update();
        try {
            const res = await this._core.json(['ping', '--json', ...ids]);
            for (const r of res.results) {
                this._pingMsgs.set(r.id, r.ok
                    ? {ok: true, text: `Pinged in ${r.seconds}s — session timer started`}
                    : {ok: false, text: `Ping failed: ${r.error ?? 'unknown error'}`});
            }
        } catch (e) {
            ids.forEach(id => this._pingMsgs.set(id, {ok: false, text: `Ping failed: ${e.message}`}));
        }
        if (this._destroyed)
            return;
        ids.forEach(id => this._pinging.delete(id));
        await this._load(['--cached']);
        GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 12, () => {
            if (!this._destroyed) {
                ids.forEach(id => this._pingMsgs.delete(id));
                this._update();
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _startLogin(acc) {
        this._loginProc?.force_exit();
        this._login = {id: acc.id, label: acc.label, message: 'Starting sign-in…'};
        try {
            this._loginProc = this._core.login(acc.id, ev => this._onLoginEvent(ev), code => this._onLoginExit(acc.id, code));
        } catch (e) {
            Object.assign(this._login, {message: e.message, failed: true});
        }
        this._update();
    }

    _onLoginEvent(ev) {
        if (!this._login || this._destroyed)
            return;
        const l = this._login;
        switch (ev.event) {
        case 'code':
            Object.assign(l, {code: ev.user_code, url: ev.verification_uri,
                message: ev.copied ? 'Code copied — paste it in the page that just opened.' : 'Enter this code in the page that just opened.'});
            break;
        case 'url':
            Object.assign(l, {url: ev.url, message: 'Finish signing in in your browser…'});
            break;
        case 'log':
            if (!l.code)
                l.message = ev.line;
            break;
        case 'done':
            Object.assign(l, {message: ev.message ?? 'Signed in', finished: true});
            break;
        case 'error':
            Object.assign(l, {message: ev.message ?? 'Sign-in failed', failed: true});
            break;
        }
        this._update();
    }

    async _onLoginExit(id, code) {
        this._loginProc = null;
        if (this._destroyed || !this._login)
            return;
        if (code === 0) {
            this._login.finished = true;
            await this._refreshAccount(id);
            GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 4, () => {
                if (!this._destroyed && this._login?.id === id && !this._login.failed) {
                    this._login = null;
                    this._update();
                }
                return GLib.SOURCE_REMOVE;
            });
        } else if (!this._login.failed && !this._login.finished) {
            Object.assign(this._login, {failed: true, message: `Sign-in was cancelled or failed (exit ${code})`});
        }
        this._update();
    }

    _cancelLogin() {
        this._loginProc?.force_exit();
        this._loginProc = null;
        this._login = null;
        this._update();
    }

    async _addAccount() {
        const f = this._addForm;
        if (!f || f.busy)
            return;
        f.busy = true;
        f.error = null;
        this._render();
        const args = ['add', f.type, '--json'];
        if (f.label.trim())
            args.push('--label', f.label.trim());
        if ((f.group ?? '').trim())
            args.push('--group', f.group.trim());
        if (f.type === 'command')
            args.push('--command', f.command);
        if (f.type === 'copilot' && f.host.trim())
            args.push('--host', f.host.trim());
        try {
            const acc = await this._core.json(args);
            this._addForm = null;
            await this._refreshAccount(acc.id);
            const full = this._status?.accounts.find(a => a.id === acc.id);
            if (f.type !== 'command' && full)
                this._startLogin(full);
        } catch (e) {
            if (this._addForm) {
                this._addForm.busy = false;
                this._addForm.error = e.message;
            }
        }
        if (this.menu.isOpen)
            this._render();
        this._update();
    }

    async _removeAccount(id) {
        this._confirmRemove = null;
        try {
            await this._core.run(['remove', id, '--json']);
        } catch (e) {
            this._error = e.message;
        }
        await this._load(['--cached']);
    }

    // ── rendering ──

    _update() {
        const pct = this._status?.summary?.percent;
        const show = this._settings.get_boolean('show-percent') && pct !== null && pct !== undefined;
        this._panelLabel.text = show ? `${Math.round(pct)}%` : '';
        this._panelLabel.visible = show;
        // Rebuild while closed too, so the menu is never empty and opens on current data.
        // Don't rebuild it under the user's fingers while a form is being typed in.
        if (!this._addForm && !this._edit)
            this._render();
    }

    _item(child, extraClass = '') {
        const item = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false, style_class: `ct-item ${extraClass}`});
        item.add_child(child);
        this._section.addMenuItem(item);
        return item;
    }

    _render() {
        this._section.removeAll();
        const now = new Date();
        const accounts = this._status?.accounts ?? [];

        this._renderHeader(accounts);
        if (this._addForm)
            this._renderAddForm();
        if (this._edit)
            this._renderEditForm();
        if (this._login)
            this._renderLogin();
        this._section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        if (!this._status && !this._error)
            this._item(label('Loading…', 'ct-dim'));
        // One section per group (the core already sorts by group, then name).
        const grouped = (this._status?.groups ?? []).length > 0;
        let current;
        accounts.forEach((acc, i) => {
            const group = acc.group ?? null;
            if (i === 0 || group !== current) {
                current = group;
                if (grouped)
                    this._item(label((group ?? 'Other').toUpperCase(), 'ct-section-title'));
            }
            if (acc.status === 'setup')
                this._renderSetupRow(acc);
            else
                this._renderCard(acc, now);
        });

        this._section.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._renderFooter();
    }

    _renderHeader(accounts) {
        const row = new St.BoxLayout({style_class: 'ct-row', x_expand: true});
        const titles = new St.BoxLayout({vertical: true, x_expand: true});
        titles.add_child(label('Coding assistants', 'ct-header-title'));
        const s = this._status?.summary;
        if (s)
            titles.add_child(label(`Highest: ${Math.round(s.percent)}% · ${s.label}`, 'ct-dim', {style: 'max-width: 250px;'}));
        row.add_child(titles);
        const idle = accounts.filter(a => a.can_ping && a.status !== 'setup' &&
            a.metrics.some(m => m.key === 'session' && m.idle));
        if (idle.length)
            row.add_child(textButton('⚡ Ping idle', () => this._ping(idle.map(a => a.id))));
        row.add_child(iconButton('list-add-symbolic', 'Add account', () => {
            this._addForm = this._addForm ? null : {type: 'claude', label: '', command: '', host: '', group: ''};
            this._render();
        }));
        row.add_child(iconButton(this._refreshing ? 'content-loading-symbolic' : 'view-refresh-symbolic',
            'Refresh all accounts', () => this._refreshAll()));
        this._item(row);
    }

    // Text entry + one-click buttons for the existing groups.
    _groupEntry(box, target) {
        const entry = new St.Entry({hint_text: 'Group (optional) — e.g. Work, Personal', text: target.group ?? '', can_focus: true, x_expand: true});
        entry.clutter_text.connect('text-changed', () => {
            target.group = entry.get_text();
        });
        box.add_child(entry);
        const groups = this._status?.groups ?? [];
        if (groups.length) {
            const row = new St.BoxLayout({style_class: 'ct-row'});
            for (const g of groups) {
                row.add_child(textButton(g, () => {
                    target.group = g;
                    entry.set_text(g);
                }, (target.group ?? '') === g ? 'ct-btn-selected' : ''));
            }
            box.add_child(row);
        }
        return entry;
    }

    _renderAddForm() {
        const f = this._addForm;
        const box = new St.BoxLayout({vertical: true, style_class: 'ct-login'});
        const head = new St.BoxLayout({style_class: 'ct-row'});
        head.add_child(label('Add account', 'ct-title', {x_expand: true}));
        head.add_child(iconButton('window-close-symbolic', 'Close', () => {
            this._addForm = null;
            this._render();
        }));
        box.add_child(head);

        const types = new St.BoxLayout({style_class: 'ct-row'});
        for (const [type, name] of ACCOUNT_TYPES) {
            types.add_child(textButton(name, () => {
                f.type = type;
                this._render();
            }, f.type === type ? 'ct-btn-selected' : ''));
        }
        box.add_child(types);

        const entry = new St.Entry({hint_text: 'Label, e.g. Claude Max · work', text: f.label, can_focus: true, x_expand: true});
        entry.clutter_text.connect('text-changed', () => {
            f.label = entry.get_text();
        });
        entry.clutter_text.connect('activate', () => this._addAccount());
        box.add_child(entry);
        this._groupEntry(box, f);
        if (f.type === 'copilot') {
            const host = new St.Entry({hint_text: 'GitHub host: github.com or e.g. mycorp.ghe.com', text: f.host, can_focus: true, x_expand: true});
            host.clutter_text.connect('text-changed', () => {
                f.host = host.get_text();
            });
            box.add_child(host);
        }
        if (f.type === 'command') {
            const cmd = new St.Entry({hint_text: 'Command, e.g. my-usage --json', text: f.command, can_focus: true, x_expand: true});
            cmd.clutter_text.connect('text-changed', () => {
                f.command = cmd.get_text();
            });
            box.add_child(cmd);
        }
        box.add_child(label(ACCOUNT_TYPES.find(t => t[0] === f.type)[2], 'ct-dim', {wrap: true}));
        if (f.error)
            box.add_child(label(f.error, 'ct-error', {wrap: true}));
        const actions = new St.BoxLayout({style_class: 'ct-row', x_align: Clutter.ActorAlign.END});
        actions.add_child(textButton(f.busy ? 'Adding…' : (f.type === 'command' ? 'Add' : 'Add & sign in'),
            () => this._addAccount()));
        box.add_child(actions);
        this._item(box);
        GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            if (!this._destroyed && entry.get_stage())
                entry.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _startEdit(acc) {
        this._addForm = null;
        this._edit = {id: acc.id, type: acc.type, label: acc.label, host: acc.host ?? '', origHost: acc.host ?? '',
            group: acc.group ?? '', origGroup: acc.group ?? ''};
        this._render();
    }

    async _saveEdit() {
        const e = this._edit;
        if (!e)
            return;
        const args = ['edit', e.id, '--json'];
        if (e.label.trim())
            args.push('--label', e.label.trim());
        if ((e.group ?? '').trim() !== e.origGroup)
            args.push('--group', (e.group ?? '').trim());
        const hostChanged = e.type === 'copilot' && e.host.trim() !== e.origHost;
        if (hostChanged)
            args.push('--host', e.host.trim());
        this._edit = null;
        try {
            await this._core.run(args);
        } catch (err) {
            this._error = err.message;
        }
        await this._refreshAccount(e.id);
        const acc = this._status?.accounts.find(a => a.id === e.id);
        if (hostChanged && acc?.can_login)
            this._startLogin(acc);   // the old host's token was dropped
        this._update();
    }

    _renderEditForm() {
        const e = this._edit;
        const box = new St.BoxLayout({vertical: true, style_class: 'ct-login'});
        const head = new St.BoxLayout({style_class: 'ct-row'});
        head.add_child(label('Edit account', 'ct-title', {x_expand: true}));
        head.add_child(iconButton('window-close-symbolic', 'Close', () => {
            this._edit = null;
            this._render();
        }));
        box.add_child(head);
        const name = new St.Entry({hint_text: 'Name', text: e.label, can_focus: true, x_expand: true});
        name.clutter_text.connect('text-changed', () => {
            e.label = name.get_text();
        });
        name.clutter_text.connect('activate', () => this._saveEdit());
        box.add_child(name);
        this._groupEntry(box, e);
        if (e.type === 'copilot') {
            const host = new St.Entry({hint_text: 'github.com or e.g. mycorp.ghe.com', text: e.host || 'github.com', can_focus: true, x_expand: true});
            host.clutter_text.connect('text-changed', () => {
                e.host = host.get_text();
            });
            host.clutter_text.connect('activate', () => this._saveEdit());
            box.add_child(host);
            box.add_child(label('Changing the host signs this account out; you will be asked to sign in on the new host.', 'ct-dim', {wrap: true}));
        }
        const actions = new St.BoxLayout({style_class: 'ct-row', x_align: Clutter.ActorAlign.END});
        actions.add_child(textButton('Save', () => this._saveEdit()));
        box.add_child(actions);
        this._item(box);
        GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            if (!this._destroyed && name.get_stage())
                name.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _renderLogin() {
        const l = this._login;
        const box = new St.BoxLayout({vertical: true, style_class: 'ct-login'});
        const head = new St.BoxLayout({style_class: 'ct-row'});
        head.add_child(label(`Signing in: ${l.label}`, 'ct-title', {x_expand: true}));
        head.add_child(iconButton('window-close-symbolic', 'Cancel', () => this._cancelLogin()));
        box.add_child(head);
        if (l.code) {
            const row = new St.BoxLayout({style_class: 'ct-row'});
            row.add_child(label(l.code, 'ct-code'));
            row.add_child(textButton('Copy', () => copyText(l.code)));
            if (l.url)
                row.add_child(textButton('Open page', () => openUri(l.url)));
            box.add_child(row);
        } else if (l.url && !l.finished) {
            box.add_child(textButton('Open sign-in page', () => openUri(l.url)));
        }
        box.add_child(label(l.message, l.failed ? 'ct-error' : l.finished ? 'ct-ok' : 'ct-dim', {wrap: true}));
        this._item(box);
    }

    _badge(vendor) {
        const glyph = VENDOR_GLYPH[vendor] ?? '?';
        return new St.Label({
            text: glyph, style_class: `ct-badge ct-badge-${vendor}`,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER,
            style: glyph.length > 1 ? 'font-size: 8pt;' : '',
        });
    }

    _renderCard(acc, now) {
        const card = new St.BoxLayout({vertical: true, style_class: 'ct-card'});

        const head = new St.BoxLayout({style_class: 'ct-row'});
        const badge = new St.Bin({child: this._badge(acc.vendor), y_align: Clutter.ActorAlign.CENTER});
        head.add_child(badge);
        const titles = new St.BoxLayout({vertical: true, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        const titleRow = new St.BoxLayout();
        titleRow.add_child(label(acc.label, 'ct-title'));
        if (acc.plan)
            titleRow.add_child(label(acc.plan.toUpperCase(), 'ct-plan'));
        titles.add_child(titleRow);
        if (acc.identity)
            titles.add_child(label(acc.identity, 'ct-dim'));
        head.add_child(titles);
        if (acc.status === 'stale')
            head.add_child(new St.Icon({icon_name: 'dialog-warning-symbolic', style: 'color: #ffb340; icon-size: 14px;'}));
        if (acc.can_ping) {
            const idle = acc.metrics.some(m => m.key === 'session' && m.idle);
            head.add_child(iconButton(this._pinging.has(acc.id) ? 'content-loading-symbolic' : 'media-playback-start-symbolic',
                'Ping (smallest model, low effort) to start the session timer',
                () => this._ping([acc.id]), idle ? 'ct-ping-idle' : ''));
        }
        if (acc.cli)
            head.add_child(iconButton('utilities-terminal-symbolic', `Copy: ${acc.cli}`, () => copyText(acc.cli)));
        if (acc.dashboard_url)
            head.add_child(iconButton('web-browser-symbolic', 'Open usage page', () => openUri(acc.dashboard_url)));
        head.add_child(iconButton('document-edit-symbolic', 'Rename, group, host', () => this._startEdit(acc)));
        head.add_child(this._removeButton(acc));
        card.add_child(head);

        for (const m of acc.metrics)
            card.add_child(this._metricRow(m, now));
        for (const n of acc.notes)
            card.add_child(label(`• ${n}`, 'ct-dim', {wrap: true}));
        if ((acc.status === 'error' || acc.status === 'stale') && acc.error) {
            const when = acc.ok_at ? clock(new Date(acc.ok_at), now) : 'earlier';
            card.add_child(label(acc.status === 'stale' ? `Last data from ${when} — ${acc.error}` : acc.error,
                acc.status === 'stale' ? 'ct-warn' : 'ct-error', {wrap: true}));
            if (acc.hint)
                card.add_child(label(acc.hint, 'ct-mono', {wrap: true}));
        }
        const msg = this._pingMsgs.get(acc.id);
        if (msg)
            card.add_child(label(msg.text, msg.ok ? 'ct-ok' : 'ct-error', {wrap: true}));
        this._item(card);
    }

    _removeButton(acc) {
        const confirming = this._confirmRemove === acc.id;
        if (confirming) {
            return textButton('Remove?', () => this._removeAccount(acc.id), 'ct-btn-danger');
        }
        return iconButton('user-trash-symbolic', 'Remove account (click twice)', () => {
            this._confirmRemove = acc.id;
            this._render();
        });
    }

    _metricRow(m, now) {
        const box = new St.BoxLayout({vertical: true, style: 'spacing: 3px;'});
        const top = new St.BoxLayout();
        top.add_child(label(m.label, 'ct-dim', {x_expand: true}));
        top.add_child(label(m.text, 'ct-value'));
        box.add_child(top);

        // Horizontal box: the fill is packed from the left edge (a BinLayout centred it).
        const track = new St.BoxLayout({style_class: 'ct-track', x_expand: true});
        const pct = m.idle ? 0 : m.percent;
        const cls = usageClass(pct);
        if (cls && pct > 0) {
            const width = Math.max(4, Math.round(BAR_WIDTH * Math.min(pct, 100) / 100));
            track.add_child(new St.Widget({style_class: `ct-fill ${cls}`, style: `width: ${width}px;`}));
        }
        box.add_child(track);

        let reset = null;
        if (m.idle)
            reset = 'Not started — ping to start the 5h window';
        else if (m.resets_at) {
            const d = new Date(m.resets_at);
            reset = `Resets in ${countdown(d, now)} · ${clock(d, now)}`;
        }
        if (reset)
            box.add_child(label(reset, 'ct-faint'));
        return box;
    }

    _renderSetupRow(acc) {
        const row = new St.BoxLayout({style_class: 'ct-row', x_expand: true});
        row.add_child(new St.Bin({child: this._badge(acc.vendor), y_align: Clutter.ActorAlign.CENTER}));
        row.add_child(label(acc.host ? `${acc.label} · ${acc.host}` : acc.label, 'ct-dim', {x_expand: true}));
        if (acc.can_login)
            row.add_child(textButton('Sign in', () => this._startLogin(acc)));
        row.add_child(iconButton('document-edit-symbolic', 'Rename, group, host', () => this._startEdit(acc)));
        row.add_child(this._removeButton(acc));
        this._item(row);
    }

    _renderFooter() {
        const row = new St.BoxLayout({style_class: 'ct-row', x_expand: true});
        let text = this._error ? `⚠ ${this._error}` : (this._lastLoaded ? `Updated ${clock(this._lastLoaded, new Date())}` : '');
        row.add_child(label(text, this._error ? 'ct-error' : 'ct-dim', {x_expand: true, wrap: !!this._error}));
        this._item(row);
    }

    // Built once, outside the section _render() rebuilds, so the submenu stays open while data refreshes.
    _buildSettings() {
        const sub = new PopupMenu.PopupSubMenuMenuItem('Settings', true);
        sub.icon.icon_name = 'emblem-system-symbolic';
        this._pctSwitch = new PopupMenu.PopupSwitchMenuItem('Show percentage in top bar',
            this._settings.get_boolean('show-percent'));
        this._pctSwitch.connect('toggled', (_i, state) => this._settings.set_boolean('show-percent', state));
        sub.menu.addMenuItem(this._pctSwitch);
        sub.menu.addAction('Edit config file', () => {
            const path = this._status?.config_path ??
                GLib.build_filenamev([GLib.get_user_config_dir(), 'creditstracker', 'config.json']);
            openUri(GLib.filename_to_uri(path, null));
        });
        this.menu.addMenuItem(sub);
    }

    destroy() {
        this._destroyed = true;
        if (this._pollId)
            GLib.source_remove(this._pollId);
        if (this._tickId)
            GLib.source_remove(this._tickId);
        this._pollId = this._tickId = null;
        if (this._settingsChangedId)
            this._settings.disconnect(this._settingsChangedId);
        this._settingsChangedId = null;
        this._loginProc?.force_exit();
        this._core.destroy();
        super.destroy();
    }
});

export function createIndicator(ext) {
    return new Indicator(ext);
}
