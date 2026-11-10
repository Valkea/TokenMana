// TokenMana — GNOME Shell top-bar extension (GNOME 45+ / Ubuntu 24.04).
//
// Hot-reload loader. On Wayland the shell caches an extension's modules for the whole session,
// so a plain disable/enable would re-run old code. Instead, every enable() imports a fresh copy
// of indicator.js under a new file name: `./install.sh` (or disable + enable) applies updates
// without logging out. Only changes to this loader, metadata.json or the schema need a re-login.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

export default class TokenManaExtension extends Extension {
    enable() {
        const gen = this._gen = (this._gen ?? 0) + 1;
        this._loadIndicator().then(mod => {
            if (gen !== this._gen)   // disabled (or re-enabled) while importing
                return;
            this._indicator = mod.createIndicator(this);
            Main.panel.addToStatusArea(this.uuid, this._indicator);
        }).catch(e => logError(e, 'TokenMana: cannot load indicator.js'));
    }

    disable() {
        this._gen = (this._gen ?? 0) + 1;
        this._indicator?.destroy();
        this._indicator = null;
    }

    async _loadIndicator() {
        const dir = Gio.File.new_for_path(GLib.build_filenamev([GLib.get_user_runtime_dir(), 'tokenmana']));
        GLib.mkdir_with_parents(dir.get_path(), 0o700);
        // Copies of earlier loads are no longer needed once imported.
        const it = dir.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
        for (let info; (info = it.next_file(null));) {
            if (/^indicator-\w+\.js$/.test(info.get_name()))
                dir.get_child(info.get_name()).delete(null);
        }
        it.close(null);

        const copy = dir.get_child(`indicator-${GLib.uuid_string_random().replaceAll('-', '')}.js`);
        Gio.File.new_for_path(GLib.build_filenamev([this.path, 'indicator.js']))
            .copy(copy, Gio.FileCopyFlags.OVERWRITE, null, null);
        return import(copy.get_uri());
    }
}
