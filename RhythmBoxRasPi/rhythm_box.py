import os
import json
import time
import threading
import sys 
from pythonosc import udp_client

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.slider import Slider
from kivy.uix.spinner import Spinner
from kivy.uix.togglebutton import ToggleButton
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.utils import platform
from kivy.metrics import dp
from kivy.uix.textinput import TextInput

# ---------------- OSC ----------------
RASPI_IP = "192.168.18.99"   #IP por defecto
OSC_PORT_PD = 9000
OSC_PORT_DISPLAY = 9001

osc_pd_client = None
osc_disp_client = None

# ---------------- CONFIG ----------------
DEFAULT_BPM = 120
MAX_PATTERNS = 16
NUM_TRACKS = 7
NUM_STEPS = 16
if platform == "android":
    from android.storage import app_storage_path
    BASE_DIR = app_storage_path()
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PATTERN_BASE_DIR = os.path.join(BASE_DIR, "patrones")


PARAMS = {
    "filter": (-100, 100),
    "lowboost": (0, 100),
    "highboost": (0, 100),
    "driveclip": (0, 100),
    "decimator": (0, 100),
    "crushing": (0, 100),
    "downsample": (0, 100),
    "pitch": (-100, 100),
    "lfo_depth": (0, 100),
    "lfo_speed": (0, 100),
    "chorus": (0, 100),
    "flanger": (0, 100),
    "reverb": (0, 100),
    "compress": (0, 100),
    "pan": (-100, 100),
}

LFO_MODES = ["sin", "tri", "saw", "square", "vibrato", "envelope", "sample&hold"]
track_mapping = {f"Sample {i}": i for i in range(1, NUM_TRACKS + 1)}

state_lock = threading.RLock()
ui = None

state = {
    "bpm": DEFAULT_BPM,
    "swing": 0,
    "current_step": 0,
    "is_playing": False,
    "transport_status": "STOP",
    "sequencer_thread": None,
    "transport_run_id": 0,
    "current_pattern": 0,
    "next_pattern": None,
    "pattern_change_pending": False,
    "edit_track": None,
    "accent_mode": False,
    "tap_times": [],
    "last_tap_time": None
}

tracks = {}
for i in range(1, NUM_TRACKS + 1):
    track_name = f"Sample {i}"
    tracks[track_name] = {
        "sequence": [0] * NUM_STEPS,
        "accent": [1] * NUM_STEPS,
        "mute": False,
        "fill_mode": False,
        "sequence_length": NUM_STEPS,
        "current_page": 0,
        "lfo_mode": 0
    }
    for p in PARAMS:
        tracks[track_name][p] = 0


# ---------------- HELPERS GENERALES ----------------
def init_osc_clients(ip):
    global osc_pd_client, osc_disp_client, RASPI_IP
    RASPI_IP = ip
    osc_pd_client = udp_client.SimpleUDPClient(ip, OSC_PORT_PD)
    osc_disp_client = udp_client.SimpleUDPClient(ip, OSC_PORT_DISPLAY)
    
IP_FILE = os.path.join(PATTERN_BASE_DIR, "osc_ip.txt")

def load_saved_ip():
    try:
        if os.path.exists(IP_FILE):
            with open(IP_FILE, "r") as f:
                return f.read().strip()
    except Exception:
        pass
    return RASPI_IP  # IP por defecto si no hay archivo

def save_ip(ip):
    try:
        os.makedirs(PATTERN_BASE_DIR, exist_ok=True)
        with open(IP_FILE, "w") as f:
            f.write(ip)
    except Exception as e:
        print("Error guardando IP:", e)

def safe_send_osc(client, address, payload=None):
    try:
        if payload is None:
            client.send_message(address, [])
        else:
            client.send_message(address, payload)
        return True
    except Exception as e:
        print(f"OSC send error {address}: {e}")
        return False


def ui_call(fn, *args, **kwargs):
    if ui is None:
        return
    Clock.schedule_once(lambda dt: fn(*args, **kwargs), 0)


def send_display(oled_text: str, ui_text=None):
    safe_send_osc(osc_disp_client, "/display", str(oled_text))
    if ui is not None:
        ui_call(ui.show_oled, str(ui_text if ui_text is not None else oled_text))

def sync_initial_display():
    with state_lock:
        bpm = state["bpm"]
        status = state["transport_status"]
        pattern = state["current_pattern"]

    # Primero la línea del patrón, luego el estado de transporte
    safe_send_osc(osc_disp_client, "/display", f"Pattern: {pattern + 1}")
    time.sleep(0.05)  # pequeño hueco para que raspi procese en orden
    safe_send_osc(osc_disp_client, "/display", f"{status} | BPM {bpm}")


def get_global_sequence_length():
    with state_lock:
        return max(track["sequence_length"] for track in tracks.values())


def get_step_delay(step_index=None):
    with state_lock:
        bpm = max(1, int(state["bpm"]))
        swing = int(state["swing"])
        if step_index is None:
            step_index = state["current_step"]

    base_delay = 60.0 / bpm / 4.0
    if step_index % 2 == 1:
        return base_delay * (1 + swing / 100.0)
    return base_delay * (1 - swing / 100.0)


def request_editor_refresh():
    if ui is not None:
        ui_call(ui.update_editor_grid)


def request_tracks_refresh():
    if ui is not None:
        ui_call(ui.refresh_tracks_ui)


def request_pattern_text_update(text):
    if ui is not None:
        ui_call(ui.set_pattern_text_safely, text)


def sync_loaded_pattern_to_ui():
    if ui is None:
        return
    ui_call(ui.apply_loaded_pattern_payload, get_ui_sync_payload())


def get_pattern_dir(pattern_index):
    return os.path.join(PATTERN_BASE_DIR, f"patron{pattern_index + 1}")


def normalize_sequence(values, target_length, fill_value):
    values = list(values) if values is not None else []
    if len(values) < target_length:
        values.extend([fill_value] * (target_length - len(values)))
    return values[:target_length]


def apply_pattern_data(data):
    with state_lock:
        if data:
            for name, info in data.items():
                if name not in tracks:
                    continue

                tr = tracks[name]
                sequence = info.get("sequence", tr["sequence"])
                sequence_length = int(info.get("sequence_length", len(sequence)))
                sequence_length = max(NUM_STEPS, sequence_length)
                sequence = normalize_sequence(sequence, sequence_length, 0)
                accent = normalize_sequence(info.get("accent", tr["accent"]), sequence_length, 1)

                tr.update({
                    "sequence": sequence,
                    "accent": accent,
                    "mute": info.get("mute", tr["mute"]),
                    "sequence_length": sequence_length,
                    "current_page": 0,
                    "lfo_mode": info.get("lfo_mode", tr["lfo_mode"])
                })
                for p in PARAMS:
                    tr[p] = info.get(p, tr.get(p, 0))

            settings = data.get("_settings", {})
            state["bpm"] = settings.get("bpm", state["bpm"])
            state["swing"] = settings.get("swing", state["swing"])
        else:
            for tr in tracks.values():
                tr["current_page"] = 0
        if state["current_step"] >= max(track["sequence_length"] for track in tracks.values()):
            state["current_step"] = 0


def send_pattern_state_to_pd(pattern_index):
    safe_send_osc(osc_pd_client, "/pattern", pattern_index + 1)

    with state_lock:
        snapshot = []
        for name, tr in tracks.items():
            snapshot.append((
                track_mapping[name],
                {p: tr[p] for p in PARAMS},
                tr["lfo_mode"],
            ))
    for track_idx, params, lfo_mode in snapshot:
        for param_name, value in params.items():
            safe_send_osc(osc_pd_client, f"/{param_name}", [track_idx, value / 100.0])
        safe_send_osc(osc_pd_client, "/lfo_mode", [track_idx, lfo_mode])
        send_display(f"Pattern: {pattern_index + 1}")


def get_ui_sync_payload():
    with state_lock:
        first_track = next(iter(tracks.values()))
        return {
            "bpm": state["bpm"],
            "swing": state["swing"],
            "length_multiplier": max(1, first_track["sequence_length"] // 16),
        }


def update_effect_controls():
    with state_lock:
        edit_track = state["edit_track"]
        lfo_mode = tracks[edit_track]["lfo_mode"] if edit_track else 0
    if edit_track and ui is not None:
        ui_call(ui.set_param_sliders, edit_track)
        ui_call(ui.set_lfo_label, LFO_MODES[lfo_mode])


# ---------------- BPM / SWING / TAP ----------------
def update_bpm(val):
    with state_lock:
        state["bpm"] = int(val)
        bpm = state["bpm"]
        status = state["transport_status"]
        
    send_display(f"{status} | BPM {bpm}")


def update_swing(val):
    with state_lock:
        state["swing"] = int(val)
        swing = state["swing"]
    safe_send_osc(osc_pd_client, "/swing", swing)
    send_display(f"Swing: {swing}")


def tap_tempo():
    now = time.perf_counter()

    with state_lock:
        if state["last_tap_time"] is None:
            state["last_tap_time"] = now
            state["tap_times"] = []
            send_display("Tap 1/4")
            return

        interval = now - state["last_tap_time"]
        state["last_tap_time"] = now

        if interval > 2.0:
            state["tap_times"] = []
            send_display("Tap 1/4")
            return

        state["tap_times"].append(interval)
        tap_number = len(state["tap_times"]) + 1

    send_display(f"Tap {tap_number}/4")

    with state_lock:
        if len(state["tap_times"]) < 4:
            return

        avg_interval = sum(state["tap_times"]) / len(state["tap_times"])
        new_bpm = int(60.0 / avg_interval)
        new_bpm = max(40, min(300, new_bpm))
        state["bpm"] = new_bpm
        state["tap_times"] = []
        state["last_tap_time"] = None

    if ui is not None:
        ui_call(ui.set_bpm_value_safely, new_bpm)
    send_display(f"BPM: {new_bpm}")


# ---------------- TRACK PARAMS ----------------
def update_track_param(param, val):
    with state_lock:
        track_name = state["edit_track"]
    if not track_name:
        return

    try:
        value = float(val)
    except Exception:
        value = 0.0

    with state_lock:
        tracks[track_name][param] = value
        track_idx = track_mapping[track_name]

    safe_send_osc(osc_pd_client, f"/{param}", [track_idx, value / 100.0])
    send_display(f"{param}: {int(value)}")


def cycle_lfo_mode():
    with state_lock:
        track_name = state["edit_track"]
        if not track_name:
            return
        track = tracks[track_name]
        track["lfo_mode"] = (track["lfo_mode"] + 1) % len(LFO_MODES)
        mode_index = track["lfo_mode"]
        mode_name = LFO_MODES[mode_index]
        track_idx = track_mapping[track_name]

    safe_send_osc(osc_pd_client, "/lfo_mode", [track_idx, mode_index])
    if ui is not None:
        ui_call(ui.set_lfo_label, mode_name)
    send_display(f"LFO: {mode_name}")


def toggle_accent_mode():
    with state_lock:
        state["accent_mode"] = not state["accent_mode"]
        enabled = state["accent_mode"]
    request_editor_refresh()
    send_display(f"Accent: {'ON' if enabled else 'OFF'}")


# ---------------- CARGA / GUARDADO ----------------
def load_pattern(pattern_index):
    pattern_dir = get_pattern_dir(pattern_index)
    seq_path = os.path.join(pattern_dir, "secuencia.json")

    data = None
    if os.path.exists(seq_path):
        try:
            with open(seq_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print("Error leyendo JSON:", e)

    apply_pattern_data(data)
    send_pattern_state_to_pd(pattern_index)
    send_display(f"Pattern: {pattern_index + 1}")
    sync_loaded_pattern_to_ui()
    
    with state_lock:
        status = state["transport_status"]
        bpm = state["bpm"]

    send_display(f"{status} | BPM {bpm}")


def save_pattern(pattern_index):
    os.makedirs(PATTERN_BASE_DIR, exist_ok=True)
    pattern_dir = get_pattern_dir(pattern_index)
    os.makedirs(pattern_dir, exist_ok=True)

    with state_lock:
        data = {}
        for name, tr in tracks.items():
            track_data = {
                "sequence": list(tr["sequence"]),
                "accent": list(tr["accent"]),
                "mute": tr["mute"],
                "sequence_length": tr["sequence_length"],
                "lfo_mode": tr["lfo_mode"],
            }
            for p in PARAMS:
                track_data[p] = tr.get(p, 0)
            data[name] = track_data
        data["_settings"] = {"bpm": state["bpm"], "swing": state["swing"]}

    try:
        with open(os.path.join(pattern_dir, "secuencia.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        send_display(f"Pattern {pattern_index + 1} guardado")
    except Exception as e:
        print("Error guardando patrón:", e)


# ---------------- TRACK UI / STATE ----------------
def toggle_mute(track_name):
    with state_lock:
        tracks[track_name]["mute"] = not tracks[track_name]["mute"]
        is_muted = tracks[track_name]["mute"]
        track_idx = track_mapping[track_name]
    request_tracks_refresh()
    send_display(f"Mute {track_name.split()[-1]}: {'ON' if is_muted else 'OFF'}")


def set_edit_track(track_name):
    with state_lock:
        state["edit_track"] = track_name
    if ui is not None:
        ui_call(ui.set_editing_track, track_name)
    update_effect_controls()
    request_editor_refresh()


def update_all_track_lengths_from_selector(multiplier):
    try:
        multiplier = int(multiplier)
    except Exception:
        multiplier = 1

    new_length = max(1, multiplier) * 16

    with state_lock:
        for tr in tracks.values():
            tr["sequence"] = normalize_sequence(tr["sequence"], new_length, 0)
            tr["accent"] = normalize_sequence(tr["accent"], new_length, 1)
            tr["sequence_length"] = new_length
            tr["current_page"] = min(tr["current_page"], (new_length // 16) - 1)

        if state["current_step"] >= new_length:
            state["current_step"] = 0

    request_tracks_refresh()
    request_editor_refresh()


def flash_track_label(name):
    if ui is not None:
        ui_call(ui.flash_label, name)


# ---------------- SECUENCIADOR ----------------
def build_step_snapshot(current_step):
    with state_lock:
        snapshot = {}
        for name, tr in tracks.items():
            if current_step < tr["sequence_length"]:
                step_active = tr["sequence"][current_step] == 1
                accent_value = tr["accent"][current_step]
            else:
                step_active = False
                accent_value = 1
                
            snapshot[name] = {
                "sequence_length": tr["sequence_length"],
                "mute": tr["mute"],
                "fill_mode": tr["fill_mode"],
                "step_active": step_active,
                "accent_value": accent_value,
            }
        return snapshot

def transport_is_active(run_id):
    with state_lock:
        return state["is_playing"] and state["transport_run_id"] == run_id

def sequencer_thread(run_id):
    target_time = time.perf_counter()

    while True:
        with state_lock:
            if not state["is_playing"] or state["transport_run_id"] != run_id:
                break
            current_step = state["current_step"]

        track_snapshot = build_step_snapshot(current_step)

        for name, tr in track_snapshot.items():
            if tr["mute"] or current_step >= tr["sequence_length"]:
                continue
            if not (tr["fill_mode"] or tr["step_active"]):
                continue

            flash_track_label(name)

            acc_float = 1.0 if tr["accent_value"] else 0.5
            safe_send_osc(osc_pd_client, "/accent", [track_mapping[name], acc_float])
            safe_send_osc(osc_pd_client, "/trigger", track_mapping[name])
            
        if current_step % 4 == 0:
            if ui is not None:
                ui_call(ui.flash_bpm)

        request_editor_refresh()

        step_delay = get_step_delay(current_step)
        target_time += step_delay

        while True:
            if not transport_is_active(run_id):
                return
            remaining = target_time - time.perf_counter()
            if remaining <= 0:
                break
            time.sleep(min(0.002, remaining))

        with state_lock:
            if not state["is_playing"] or state["transport_run_id"] != run_id:
                break
            max_len = max(track["sequence_length"] for track in tracks.values())
            state["current_step"] = (state["current_step"] + 1) % max_len
            wrapped = state["current_step"] == 0
            pending_change = state["pattern_change_pending"]
            next_pattern = state["next_pattern"]

        if pending_change and wrapped:
            with state_lock:
                state["current_pattern"] = next_pattern if next_pattern is not None else state["current_pattern"]
                state["next_pattern"] = None
                state["pattern_change_pending"] = False
                new_pattern = state["current_pattern"]
            load_pattern(new_pattern)
            request_pattern_text_update(f"Patrón: {new_pattern + 1}")


def start_sequencer():
    with state_lock:
        if state["is_playing"]:
            return
        state["transport_status"] = "PLAY"
        state["transport_run_id"] += 1
        run_id = state["transport_run_id"]
        state["is_playing"] = True
        thread = threading.Thread(target=sequencer_thread, args=(run_id,), daemon=True)
        state["sequencer_thread"] = thread
    thread.start()


def pause_sequencer():
    with state_lock:
        state["is_playing"] = False
        state["transport_run_id"] += 1
        state["transport_status"] = "PAUSE"

def stop_sequencer(update_ui=True):
    with state_lock:
        state["is_playing"] = False
        state["transport_run_id"] += 1
        state["current_step"] = 0
    if update_ui:
        request_editor_refresh()


def change_pattern(direction):
    with state_lock:
        playing = state["is_playing"]

        if playing:
            if state["next_pattern"] is None:
                state["next_pattern"] = state["current_pattern"]

            state["next_pattern"] = (state["next_pattern"] + direction) % MAX_PATTERNS

            if state["next_pattern"] == state["current_pattern"]:
                state["pattern_change_pending"] = False
                state["next_pattern"] = None

                spinner_text = f"Pattern: {state['current_pattern'] + 1}"
                oled_text = spinner_text
                ui_text = spinner_text

            else:
                state["pattern_change_pending"] = True

                spinner_text = f"Pattern (pendiente): {state['next_pattern'] + 1}"
                oled_text = f"Pattern {state['current_pattern'] + 1} → {state['next_pattern'] + 1}"
                ui_text = f"Pattern {state['current_pattern'] + 1} -> {state['next_pattern'] + 1}"

        else:
            state["current_pattern"] = (
                state["current_pattern"] + direction
            ) % MAX_PATTERNS

            new_pattern = state["current_pattern"]

            spinner_text = f"Pattern: {new_pattern + 1}"
            oled_text = spinner_text
            ui_text = spinner_text

    request_pattern_text_update(spinner_text)
    send_display(oled_text, ui_text)

    if not playing:
        load_pattern(new_pattern)


# ---------------- EDITOR ----------------
def toggle_edit_step(step):
    with state_lock:
        track_name = state["edit_track"]
        accent_mode = state["accent_mode"]
        if not track_name:
            return

        tr = tracks[track_name]
        step_index = tr["current_page"] * 16 + step
        if step_index < len(tr["sequence"]):
            if accent_mode:
                tr["accent"][step_index] ^= 1
            else:
                tr["sequence"][step_index] ^= 1
    request_editor_refresh()


def change_page(direction):
    with state_lock:
        track_name = state["edit_track"]
        if not track_name:
            return
        tr = tracks[track_name]
        max_page = max(1, tr["sequence_length"] // 16)
        new_page = tr["current_page"] + direction
        if 0 <= new_page < max_page:
            tr["current_page"] = new_page
    request_editor_refresh()


def reset_sequence_view():
    with state_lock:
        state["current_step"] = 0
        for tr in tracks.values():
            tr["current_page"] = 0
    request_editor_refresh()


# ---------------- UI ----------------
class RhythmUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=8, padding=8, **kwargs)
        global ui
        ui = self

        # ---- fondo oscuro ----
        with self.canvas.before:
            Color(0.08, 0.08, 0.08, 1)
            self.bg_rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._update_bg, size=self._update_bg)

        self._updating_ui = False
        self._oled_event = None

        # ---------------- TOP ----------------
        top = BoxLayout(size_hint_y=None, height=36, spacing=4)

        self.pattern_spinner = Spinner(
            text=f"Pattern: {state['current_pattern'] + 1}",
            values=[f"Pattern: {i}" for i in range(1, MAX_PATTERNS + 1)],
            size_hint_x=0.18,
        )
        self.pattern_spinner.bind(text=self._on_pattern_spinner)

        top.add_widget(self.pattern_spinner)
        top.add_widget(Button(text="<", size_hint_x=None, width=dp(70), on_release=lambda b: change_pattern(-1)))
        top.add_widget(Button(text=">", size_hint_x=None, width=dp(70), on_release=lambda b: change_pattern(1)))
        top.add_widget(Button(text="Guardar", size_hint_x=None, width=dp(120),
                              on_release=lambda b: save_pattern(state["current_pattern"])))
        self.add_widget(top)

        # ---------------- MAIN ----------------
        main = BoxLayout(orientation="horizontal", spacing=8)

        # -------- LEFT TRACKS --------
        left = BoxLayout(orientation="vertical", size_hint_x=0.38)
        self.track_rows = {}

        top_spacer = BoxLayout(size_hint_y=1)
        bottom_spacer = BoxLayout(size_hint_y=1)

        tracks_container = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=6
        )

        for name in tracks:
            row = BoxLayout(size_hint_y=None, height=dp(56), spacing=6)

            select_btn = Button(text=name, size_hint_x=0.6,
                                background_normal="", background_down="")
            select_btn.background_color = (0.3, 0.3, 0.3, 1)

            mute_btn = Button(text="Mute", size_hint_x=0.4,
                              background_normal="", background_down="")
            mute_btn.background_color = (0.3, 0.3, 0.3, 1)

            select_btn.bind(on_release=lambda b, n=name: self._on_select_button(n))
            mute_btn.bind(on_release=lambda b, n=name: self._on_mute_button(n, b))

            row.add_widget(select_btn)
            row.add_widget(mute_btn)
            tracks_container.add_widget(row)

            self.track_rows[name] = {
                "select_btn": select_btn,
                "mute_btn": mute_btn
            }

        # ---- ALTURA DINÁMICA ----
        tracks_container.height = len(tracks) * dp(62)

        # ---- CENTRADO VERTICAL ----
        left.add_widget(top_spacer)
        left.add_widget(tracks_container)
        left.add_widget(bottom_spacer)

        # ---- IP OSC ----
        ip_box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(70), spacing=4, padding=(0, 4))
        ip_box.add_widget(Label(
            text="IP Pure Data",
            size_hint_y=None,
            height=dp(24),
            font_size=dp(16),
            color=(0.6, 0.6, 0.6, 1)
        ))
        ip_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=4)
        self.ip_input = TextInput(
            text=RASPI_IP,
            multiline=False,
            font_size=dp(20),
            size_hint_x=1,
        )
        self.ip_input.bind(on_text_validate=self._on_ip_changed)
        ip_ok_btn = Button(text="OK", size_hint_x=None, width=dp(45))
        ip_ok_btn.bind(on_release=lambda b: self._on_ip_changed(self.ip_input))
        ip_row.add_widget(self.ip_input)
        ip_row.add_widget(ip_ok_btn)
        ip_box.add_widget(ip_row)
        left.add_widget(ip_box)

        # añadir al layout principal
        main.add_widget(left)

        # -------- RIGHT --------
        right = BoxLayout(orientation="vertical", spacing=6)

        # OLED
        oled_row = BoxLayout(size_hint_y=0.12, padding=4)

        self.oled_box = BoxLayout(size_hint=(1, 1))

        with self.oled_box.canvas.before:
            Color(0, 0, 0, 1)
            self._oled_rect = Rectangle(pos=self.oled_box.pos, size=self.oled_box.size)

        with self.oled_box.canvas.after:
            Color(1, 1, 1, 1)
            from kivy.graphics import Line
            self._oled_border = Line(rectangle=(0, 0, 0, 0), width=1.2)

        self.oled_label = Label(
            text=f"Pattern: {state['current_pattern'] + 1}",
            halign="left",
            valign="middle",
            size_hint=(1, 1),
        )
        
        self.oled_box.bind(pos=self._update_oled_graphics, size=self._update_oled_graphics)


        self.oled_label.color = (1, 1, 1, 1)

        self.oled_box.add_widget(self.oled_label)
        oled_row.add_widget(self.oled_box)
        right.add_widget(oled_row)

        self.oled_box.bind(size=self._update_oled_graphics)

        # CONTROLES
        global_row = BoxLayout(size_hint_y=None, height=64, spacing=6)

        # BPM
        bpm_box = BoxLayout(orientation="vertical", size_hint_x=0.5)
        self.bpm_label = Label(text="BPM", size_hint_y=None, height=18)
        bpm_box.add_widget(self.bpm_label)

        bpm_row = BoxLayout(size_hint_y=None, height=36, spacing=4)

        self.bpm_minus = Button(text="-", size_hint_x=None, width=40)
        self.bpm_minus.bind(on_release=lambda b: self._change_bpm(-1))

        self.bpm_slider = Slider(min=40, max=300, value=state["bpm"], step=1)
        self.bpm_slider.bind(value=self._on_bpm_slider)

        self.bpm_plus = Button(text="+", size_hint_x=None, width=40)
        self.bpm_plus.bind(on_release=lambda b: self._change_bpm(1))

        bpm_row.add_widget(self.bpm_minus)
        bpm_row.add_widget(self.bpm_slider)
        bpm_row.add_widget(self.bpm_plus)
        bpm_box.add_widget(bpm_row)

        global_row.add_widget(bpm_box)

        # SWING
        swing_box = BoxLayout(orientation="vertical", size_hint_x=0.25)
        swing_box.add_widget(Label(text="Swing", size_hint_y=None, height=18))

        self.swing_slider = Slider(min=0, max=50, value=state["swing"], step=1)
        self.swing_slider.bind(value=self._on_swing_slider)

        swing_box.add_widget(self.swing_slider)
        global_row.add_widget(swing_box)

        # TRANSPORT
        transport_box = BoxLayout(orientation="vertical", size_hint_x=0.25)
        transport_box.add_widget(Label(text="Transport", size_hint_y=None, height=18))

        transport_row = BoxLayout(size_hint_y=None, height=34, spacing=6)

        self.play_btn = Button(text="Play")
        self.pause_btn = Button(text="Pause")
        self.stop_btn = Button(text="Stop")
        self.tap_btn = Button(text="TAP")

        self.play_btn.bind(on_release=self._on_play)
        self.pause_btn.bind(on_release=self._on_pause)
        self.stop_btn.bind(on_release=self._on_stop)
        self.tap_btn.bind(on_press=self._on_tap_press)
        self.tap_btn.bind(on_release=self._on_tap_release)

        transport_row.add_widget(self.play_btn)
        transport_row.add_widget(self.pause_btn)
        transport_row.add_widget(self.stop_btn)
        transport_row.add_widget(self.tap_btn)

        transport_box.add_widget(transport_row)
        global_row.add_widget(transport_box)

        right.add_widget(global_row)

        # LENGTH
        length_row = BoxLayout(size_hint_y=None, height=36, spacing=6)

        lbl = Label(
            text="Longitud (x16 pasos):",
            size_hint_x=0.6,
            halign="right",     
            valign="middle"
        )

        lbl.bind(size=lambda *x: setattr(lbl, "text_size", lbl.size))

        length_row.add_widget(lbl)

        self.length_spinner = Spinner(
            text="1",
            values=("1", "2", "3", "4"),
            size_hint_x=0.2
        )
        self.length_spinner.bind(text=self._on_length_spinner)

        length_row.add_widget(self.length_spinner)
        right.add_widget(length_row)

        # ---------------- EDITOR ----------------
        editor = BoxLayout(orientation="vertical", size_hint_y=1, spacing=6)

        # HEADER
        editor_header = BoxLayout(size_hint_y=None, height=30, spacing=6)
        self.edit_track_label = Label(text="Selecciona una pista")
        editor_header.add_widget(self.edit_track_label)

        self.accent_btn = Button(text="Accent", size_hint_x=None, width=100)
        self.accent_btn.bind(on_release=self._on_accent_btn)

        self.fill_btn = Button(text="Fill", size_hint_x=None, width=80)
        self.fill_btn.bind(on_press=self._on_fill_press)
        self.fill_btn.bind(on_release=self._on_fill_release)

        editor_header.add_widget(self.fill_btn)
        editor_header.add_widget(self.accent_btn)

        page_nav = BoxLayout(size_hint_x=None, width=120, spacing=4)
        page_nav.add_widget(Button(text="<", on_release=lambda b: change_page(-1)))
        page_nav.add_widget(Button(text=">", on_release=lambda b: change_page(1)))
        editor_header.add_widget(page_nav)

        editor.add_widget(editor_header)

        # GRID (más grande)
        self.editor_grid = GridLayout(cols=16, size_hint_y=None, height=80, spacing=2)
        self.editor_buttons = []

        for i in range(16):
            btn = ToggleButton(text=str(i + 1))
            btn.bind(on_release=lambda b, idx=i: toggle_edit_step(idx))
            self.editor_grid.add_widget(btn)
            self.editor_buttons.append(btn)

        editor.add_widget(self.editor_grid)

        # LFO
        lfo_row = BoxLayout(size_hint_y=None, height=40, spacing=6)
        lfo_row.add_widget(Button(text="Cambiar LFO", on_release=lambda b: cycle_lfo_mode()))

        self.lfo_mode_label = Label(text=f"LFO: {LFO_MODES[0]}", size_hint_x=0.4)
        lfo_row.add_widget(self.lfo_mode_label)

        editor.add_widget(lfo_row)

        # PARAMS (FIX CLAVE)
        params_container = GridLayout(cols=3, spacing=6, size_hint_y=1)

        self.param_sliders = {}
        for p, (mn, mx) in PARAMS.items():
            col = BoxLayout(orientation="vertical")
            col.add_widget(Label(text=p.capitalize(), size_hint_y=None, height=18))

            slider = Slider(min=mn, max=mx, value=0, step=1)
            slider.bind(value=lambda sl, val, param=p: self._on_param_slider(param, val))

            col.add_widget(slider)
            params_container.add_widget(col)
            self.param_sliders[p] = slider

        editor.add_widget(params_container)

        right.add_widget(editor)

        # ================= INIT =================
        main.add_widget(right)
        self.add_widget(main)

        first = next(iter(tracks))
        set_edit_track(first)

        Clock.schedule_once(lambda dt: self.refresh_tracks_ui(), 0)
        Clock.schedule_once(lambda dt: self.update_editor_grid(), 0)

    # ---- helpers UI safe updates ----
    def set_pattern_text_safely(self, text):
        self._updating_ui = True
        try:
            self.pattern_spinner.text = text
        finally:
            self._updating_ui = False

    def set_length_value_safely(self, text):
        self._updating_ui = True
        try:
            self.length_spinner.text = text
        finally:
            self._updating_ui = False

    def set_bpm_value_safely(self, bpm):
        self._updating_ui = True
        try:
            self.bpm_slider.value = bpm
        finally:
            self._updating_ui = False

    def apply_loaded_pattern_payload(self, payload):
        self._updating_ui = True
        try:
            self.bpm_slider.value = payload["bpm"]
            self.swing_slider.value = payload["swing"]
            self.length_spinner.text = str(payload["length_multiplier"])
        finally:
            self._updating_ui = False
        self.refresh_tracks_ui()
        self.update_editor_grid()
        with state_lock:
            edit_track = state["edit_track"]
            lfo_mode = tracks[edit_track]["lfo_mode"] if edit_track else 0
        if edit_track:
            self.set_param_sliders(edit_track)
            
        self.set_lfo_label(LFO_MODES[lfo_mode])

    def set_param_sliders(self, track_name):
        self._updating_ui = True
        try:
            with state_lock:
                values = {p: tracks[track_name].get(p, 0) for p in self.param_sliders}
            for p, slider in self.param_sliders.items():
                slider.value = values[p]
        finally:
            self._updating_ui = False

    # ---- OLED ----
    def _update_oled_graphics(self, *args):
        w, h = self.oled_box.size

        # fondo
        self._oled_rect.pos = self.oled_box.pos
        self._oled_rect.size = self.oled_box.size

        # borde
        self._oled_border.rectangle = (self.oled_box.x, self.oled_box.y, w, h)

        # text_size siempre actualizado
        self.oled_label.text_size = (w - 10, h)

        # FONT DINÁMICO
        self.oled_label.font_size = max(12, min(w, h) * 0.35)

        # fuerza repaint
        self.oled_label.texture_update()
    # ================= OLED TEXT =================
    def show_oled(self, text: str, timeout: float = 3):
        if self._oled_event:
            Clock.unschedule(self._oled_event)

        self.oled_label.text = str(text)
        self._oled_event = lambda dt: self._revert_oled()
        Clock.schedule_once(self._oled_event, timeout)

    def _revert_oled(self):
        self.oled_label.text = f"Pattern: {state['current_pattern'] + 1}"
        self._oled_event = None

    # ---- callbacks UI ----
    def _on_ip_changed(self, instance):
        new_ip = self.ip_input.text.strip()
        if not new_ip:
            return
        init_osc_clients(new_ip)
        save_ip(new_ip)
        send_display(f"IP: {new_ip}")
    
    def _on_bpm_slider(self, slider, value):
        if self._updating_ui:
            return
        update_bpm(int(value))

    def _on_swing_slider(self, slider, value):
        if self._updating_ui:
            return
        update_swing(int(value))

    def _on_param_slider(self, param, value):
        if self._updating_ui:
            return
        update_track_param(param, value)

    def _on_length_spinner(self, spinner, text):
        if self._updating_ui:
            return
        update_all_track_lengths_from_selector(text)

    def _on_mute_button(self, name, button_widget):
        toggle_mute(name)
        with state_lock:
            is_muted = tracks[name]["mute"]
        button_widget.background_color = (0.8, 0.2, 0.2, 1) if is_muted else (0.3, 0.3, 0.3, 1)
        self.refresh_tracks_ui()

    def _on_select_button(self, name):
        set_edit_track(name)

    def refresh_tracks_ui(self):
        with state_lock:
            edit_track = state["edit_track"]
            mute_map = {name: tracks[name]["mute"] for name in tracks}

        for name, widgets in self.track_rows.items():
            widgets["select_btn"].background_color = (0.2, 0.8, 0.2, 1) if edit_track == name else (0.3, 0.3, 0.3, 1)
            widgets["mute_btn"].background_color = (0.8, 0.2, 0.2, 1) if mute_map[name] else (0.3, 0.3, 0.3, 1)

    def _on_play(self, button):
        start_sequencer()
        self.play_btn.background_color = (0.2, 0.8, 0.2, 1)
        self.pause_btn.background_color = (0.3, 0.3, 0.3, 1)
        self.stop_btn.background_color = (0.3, 0.3, 0.3, 1)
        send_display(f"PLAY | BPM {state['bpm']}")

    def _on_pause(self, button):
        pause_sequencer()
        self.play_btn.background_color = (0.3, 0.3, 0.3, 1)
        self.pause_btn.background_color = (0.8, 0.5, 0.0, 1)
        self.stop_btn.background_color = (0.3, 0.3, 0.3, 1)
        send_display(f"PAUSE | BPM {state['bpm']}")

    def _on_stop(self, button):
        stop_sequencer()
        self.play_btn.background_color = (0.3, 0.3, 0.3, 1)
        self.pause_btn.background_color = (0.3, 0.3, 0.3, 1)
        self.stop_btn.background_color = (0.8, 0.2, 0.2, 0.8)
        send_display(f"STOP | BPM {state['bpm']}")

    def _change_bpm(self, delta):
        with state_lock:
            new_bpm = max(40, min(300, state["bpm"] + delta))
        self.bpm_slider.value = new_bpm

    def _on_tap_press(self, button):
        button.background_color = (1, 0, 0, 1)

    def _on_tap_release(self, button):
        button.background_color = (0.3, 0.3, 0.3, 1)
        tap_tempo()

    def _on_pattern_spinner(self, spinner, text):
        if self._updating_ui:
            return

        try:
            if "pendiente" in text.lower():
                return
            new_pattern = int(text.split(":")[1].strip()) - 1
        except Exception:
            return

        with state_lock:
            current_pattern = state["current_pattern"]
            playing = state["is_playing"]

        if new_pattern == current_pattern:
            return

        if playing:
            with state_lock:
                state["next_pattern"] = new_pattern
                state["pattern_change_pending"] = True
                current = state["current_pattern"] + 1
                selected = new_pattern + 1

            spinner_text = f"Pattern (pendiente): {selected}"
            oled_text = f"Pattern {current} -> {selected}"

            request_pattern_text_update(spinner_text)          
            send_display(oled_text, oled_text)                 

        else:
            with state_lock:
                state["current_pattern"] = new_pattern
            load_pattern(new_pattern)


    def set_lfo_label(self, mode_name):
        self.lfo_mode_label.text = f"LFO: {mode_name}"

    def _on_accent_btn(self, button):
        toggle_accent_mode()
        with state_lock:
            accent_mode = state["accent_mode"]
        button.background_color = (0.2, 0.8, 0.2, 1) if accent_mode else (0.3, 0.3, 0.3, 1)

    def _on_fill_press(self, button):
        with state_lock:
            edit_track = state["edit_track"]
            if not edit_track:
                return
            tracks[edit_track]["fill_mode"] = True
        button.background_color = (0.2, 0.8, 0.2, 1)
        send_display(f"Fill {edit_track.split()[-1]}")

    def _on_fill_release(self, button):
        with state_lock:
            edit_track = state["edit_track"]
            if not edit_track:
                return
            tracks[edit_track]["fill_mode"] = False
            current_pattern = state["current_pattern"] + 1
        button.background_color = (0.3, 0.3, 0.3, 1)
        send_display(f"Pattern: {current_pattern}")

    def set_editing_track(self, track_name):
        self.edit_track_label.text = f"Editando: {track_name}"
        self.set_param_sliders(track_name)
        self.update_editor_grid()
        self.refresh_tracks_ui()
        with state_lock:
            fill_active = tracks[track_name]["fill_mode"]
            lfo_mode = tracks[track_name]["lfo_mode"]
        self.fill_btn.background_color = (0.2, 0.8, 0.2, 1) if fill_active else (0.3, 0.3, 0.3, 1)
        self.set_lfo_label(LFO_MODES[lfo_mode])

    def update_bpm_and_swing(self, bpm, swing):
        self.apply_loaded_pattern_payload({
            "bpm": bpm,
            "swing": swing,
            "length_multiplier": int(self.length_spinner.text or "1"),
        })

    def flash_label(self, name):
        if name in self.track_rows:
            btn = self.track_rows[name]["select_btn"]
            btn.background_color = (1, 1, 0, 1)
            Clock.schedule_once(lambda dt: self._restore_btn_color(name), 0.1)
    def flash_bpm(self):
        self.bpm_label.color = (1, 0, 0, 1)
        Clock.schedule_once(lambda dt: setattr(self.bpm_label, "color", (1, 1, 1, 1)), 0.1)

    def _restore_btn_color(self, name):
        if name in self.track_rows:
            with state_lock:
                selected = state["edit_track"] == name
            btn = self.track_rows[name]["select_btn"]
            btn.background_color = (0.2, 0.8, 0.2, 1) if selected else (0.3, 0.3, 0.3, 1)

    def update_editor_grid(self, *args):
        with state_lock:
            edit_track = state["edit_track"]
            accent_mode = state["accent_mode"]
            current_step = state["current_step"]
            if not edit_track:
                return
            tr = tracks[edit_track]
            page = tr["current_page"]
            sequence = list(tr["sequence"])
            accent = list(tr["accent"])

        for i, btn in enumerate(self.editor_buttons):
            step_index = page * 16 + i
            btn.text = str(step_index + 1)

            if step_index < len(sequence):
                active = sequence[step_index]
                is_current = step_index == current_step

                if accent_mode:
                    btn.state = "down" if accent[step_index] else "normal"
                    btn.background_color = (0.4, 0.6, 1, 1) if accent[step_index] else (0.8, 0.8, 1, 1)
                else:
                    btn.state = "down" if active else "normal"
                    if is_current and active:
                        btn.background_color = (1, 0.6, 0.2, 1)
                    elif is_current:
                        btn.background_color = (1, 0.4, 0.4, 1)
                    elif active:
                        btn.background_color = (0, 0.8, 0, 1)
                    else:
                        btn.background_color = (0.3, 0.3, 0.3, 1)
                btn.disabled = False
            else:
                btn.disabled = True
                btn.state = "normal"
                btn.background_color = (0.6, 0.6, 0.6, 1)
                
    def _update_bg(self, *args):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size


# ---------------- APP ----------------
    
class RhythmApp(App):
    def build(self):
        if sys.platform != 'android':
            Window.size = (1200, 750)
        saved_ip = load_saved_ip()
        init_osc_clients(saved_ip)
        self.ui = RhythmUI()
        load_pattern(state["current_pattern"])
        sync_initial_display()
        Window.bind(on_request_close=self._on_request_close)
        return self.ui
    
    def on_start(self):
        Clock.schedule_once(lambda dt: sync_initial_display(), 0.5)

    def _on_request_close(self, *args):
        # 1. Marcar shutdown para que todos los hilos y callbacks paren
        with state_lock:
            state["is_playing"] = False
            state["transport_run_id"] += 1
            state["transport_status"] = "STOP"

        # 2. Parar el secuenciador
        stop_sequencer(update_ui=False)

        # 3. Dar un momento al hilo del secuenciador para que salga
        seq_thread = state.get("sequencer_thread")
        if seq_thread and seq_thread.is_alive():
            seq_thread.join(timeout=0.5)

        # 4. Mandar BYE al display
        try:
            safe_send_osc(osc_disp_client, "/display", "BYE")
        except Exception as e:
            print("Error enviando BYE:", e)

        # 5. Matar el proceso directamente — Kivy no cierra limpio en todos los sistemas
        import os
        os._exit(0)


if __name__ == "__main__":
    RhythmApp().run()
