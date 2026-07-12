#!/usr/bin/env python3
"""
raspi_control.py
Script para Raspberry Pi que:

1. Lee potencimetros en MCP3008 (via SPI).
2. Enva valores normalizados a Pure Data en /volumen (localhost:9000).
3. Corre servidor OSC en puerto 9001 para mostrar mensajes en pantalla OLED SH1106/SSD1306.
4. Actualiza display OLED con textos enviados por Kivy.

---

?? Dependencias:
    sudo apt update
    sudo apt install python3-pip python3-dev libopenjp2-7 libtiff6
    pip3 install python-osc luma.oled spidev RPi.GPIO gpiozero


    ACTIVAR

    sudo raspi-config
 Navega a:
Interface Options / SPI / Enable

Confirma y luego reinicia:

LO MISMO CON I2C


?? Wiring MCP3008:
    MCP3008 VDD  -> 3.3V
    MCP3008 VREF -> 3.3V
    MCP3008 AGND -> GND
    MCP3008 DGND -> GND
    MCP3008 CLK  -> GPIO11 (SPI0 SCLK)
    MCP3008 DOUT -> GPIO9  (SPI0 MISO)
    MCP3008 DIN  -> GPIO10 (SPI0 MOSI)
    MCP3008 CS   -> GPIO8  (SPI0 CE0)
    Potencimetros conectados a CH0..CH7 y GND/3.3V.

/// Pantalla OLED (I2C):
    SDA -> GPIO2
    SCL -> GPIO3
    VCC -> 3.3V
    GND -> GND
"""

import time
import spidev
import threading
from pythonosc import udp_client, dispatcher, osc_server
from luma.core.interface.serial import i2c
from luma.oled.device import sh1106, ssd1306
from luma.core.render import canvas
from PIL import ImageFont

shutdown = False  #para solucionar bug oled al cerrar, al cambiar parametro y cerrar instantaneo se borraba el see you soon

# ---------------- CONFIG ----------------
OSC_PD_PORT = 9000
OSC_DISPLAY_PORT = 9001
OSC_TARGET_IP = "127.0.0.1"  # Pure Data corre en localhost

# canales que usaremos del MCP3008
NUM_CHANNELS = 8  # tengo 8 faders/knobs
DEADZONE = 5      # diferencia mnima en valor bruto (0-1023) para enviar update
MASTER_DEADZONE = 10
MASTER_CHANNEL = 0

# ---------------- MCP3008 ----------------
spi = spidev.SpiDev()
spi.open(0, 0)   # bus 0, device CE0
spi.max_speed_hz = 1350000

def read_adc(channel: int) -> int:
    """Lee un canal (0-7) del MCP3008 y devuelve 0-1023"""
    if not (0 <= channel <= 7):
        return 0
    r = spi.xfer2([1, (8+channel)<<4, 0])
    return ((r[1] & 3) << 8) | r[2]

# ---------------- OSC CLIENT (PD) ----------------
osc_pd_client = udp_client.SimpleUDPClient(OSC_TARGET_IP, OSC_PD_PORT)

# ---------------- OLED ----------------
serial = i2c(port=1, address=0x3C)
try:
    device = sh1106(serial)  # SH1106
except Exception:
    device = ssd1306(serial)  # fallback

font_line1 = ImageFont.truetype(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",14)

font_line2 = ImageFont.truetype(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",12)

# dos lneas
current_pattern_text = "Patron: 1"
current_param_text   = ""
default_param_text = "STOP | BPM 120"
clear_timer = None

oled_lock = threading.Lock()
timer_lock = threading.Lock()

def oled_refresh_loop():
    """Refresca la pantalla OLED a ~20 FPS."""
    while True:
        with oled_lock:
            line1 = current_pattern_text
            line2 = current_param_text
        with canvas(device) as draw:
            draw.text((0, 0), line1, font=font_line1, fill=255)
            draw.text((0, 20), line2, font=font_line2, fill=255)
        time.sleep(0.05)  # 20 actualizaciones por segundo

# setters actualizan solo las variables (no redibujan directamente)
def set_pattern_text(text: str):
    global current_pattern_text
    with oled_lock:
        current_pattern_text = text

clear_timer = None

def clear_param_text():
    global current_param_text
    if shutdown:         
        return
    with oled_lock:
        current_param_text = default_param_text

def set_default_status(text: str):
    global default_param_text, current_param_text

    with oled_lock:
        default_param_text = text
        current_param_text = text

def set_param_text(text: str):
    global current_param_text, clear_timer

    with timer_lock:
        with oled_lock:
            current_param_text = text

        if clear_timer and clear_timer.is_alive():
            clear_timer.cancel()

        clear_timer = threading.Timer(2.0, clear_param_text)
        clear_timer.start()

# ---------------- OSC SERVER (DISPLAY) ----------------
def osc_display_handler(addr, *args):
    msg = " ".join(map(str, args))
    if "bye" in msg.lower():
        global current_pattern_text, current_param_text, shutdown
        shutdown = True                              
        if clear_timer and clear_timer.is_alive():
            clear_timer.cancel()
        with oled_lock:
            current_pattern_text = "BYE"
            current_param_text   = "See you soon"
        return
    if "pattern" in msg.lower():
        set_pattern_text(msg)
        return
    elif "play" in msg.lower() or "stop" in msg.lower() or "pause" in msg.lower():
        set_default_status(msg)
        return
    set_param_text(msg)
    
disp = dispatcher.Dispatcher()
disp.map("/display", osc_display_handler)


def start_osc_server():
    server = osc_server.ThreadingOSCUDPServer(("0.0.0.0", OSC_DISPLAY_PORT), disp)
    print(f"Servidor OSC Display escuchando en puerto {OSC_DISPLAY_PORT}")
    server.serve_forever()

# ---------------- LECTURA MCP3008 LOOP ----------------
def mcp3008_loop():
    last_vals = [0]*NUM_CHANNELS
    
    while True:

        for ch in range(NUM_CHANNELS):
            val = read_adc(ch)

            dz = MASTER_DEADZONE if ch == MASTER_CHANNEL else DEADZONE

            if abs(val - last_vals[ch]) > dz:
                last_vals[ch] = val
                norm_val = round(val / 1023.0, 2)

                try:
                    if ch == MASTER_CHANNEL:
                        osc_pd_client.send_message("/master_vol", norm_val)
                        set_param_text(f"Master {int(norm_val*100)}%")
                    else:
                        track = ch
                        osc_pd_client.send_message("/volumen", [track, norm_val])

                except Exception as e:
                    print("OSC send error:", e)

        time.sleep(0.05)  # ~20Hz de muestreo

# ---------------- MAIN ----------------
if __name__ == "__main__":
    print("Iniciando raspi_control.py ...")
    set_pattern_text("Pattern: 1")
    
    # hilo OLED
    t_oled = threading.Thread(target=oled_refresh_loop, daemon=True)
    t_oled.start()

    # lanzar hilo OSC server
    t_osc = threading.Thread(target=start_osc_server, daemon=True)
    t_osc.start()

    # loop de lectura MCP3008
    mcp3008_loop()


