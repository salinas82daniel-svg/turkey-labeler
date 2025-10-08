#!/usr/bin/env python3
"""
LD Turkey Labeler - With Advanced Options Window

Changes:
- Moved Scale and Printer COM/baud controls into an "Advanced Options" submenu.
- Main GUI is simplified (Template, Product, Weight, Sell-by, Lot, Preview/Print).
- Advanced Options window keeps all previous functionality.
"""

import os
import sys
import json
import sqlite3
import threading
import time
import re
from datetime import datetime

try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

try:
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.graphics.barcode import createBarcodeDrawing
    from reportlab.lib.utils import ImageReader
except Exception:
    createBarcodeDrawing = None
    ImageReader = None

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception:
    print("Tkinter required. On Debian/Ubuntu: sudo apt-get install python3-tk")
    sys.exit(1)

# ---------------- helpers ----------------
def resource_path(rel_path):
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel_path)

DB_FILE = "products.db"
TEMPLATES_DIR = resource_path("templates")
SETTINGS_FILE = "settings.json"
LABEL_WIDTH_IN = 2.0
LABEL_HEIGHT_IN = 2.0

# ---------------- templates ----------------
def ensure_templates():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    sample_path = os.path.join(TEMPLATES_DIR, "default_2x2.json")
    if not os.path.exists(sample_path):
        sample = {
            "name": "default_2x2",
            "size_in": [2.0, 2.0],
            "font": "Helvetica",
            "fields": [
                {"name": "logo", "x": 0.1, "y": 1.6, "width": 0.8, "height": 0.4, "path": "logo.png"},
                {"name": "product_name", "x": 0.1, "y": 1.35, "size": 10},
                {"name": "weight", "x": 0.1, "y": 1.1, "size": 9},
                {"name": "price_per_lb", "x": 0.1, "y": 0.9, "size": 9},
                {"name": "total_price", "x": 0.1, "y": 0.7, "size": 12},
                {"name": "sell_by", "x": 0.1, "y": 0.5, "size": 8},
                {"name": "lot", "x": 0.1, "y": 0.3, "size": 8},
                {"name": "barcode", "x": 0.1, "y": 0.02, "width": 1.8, "height": 0.45}
            ]
        }
        with open(sample_path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2)

def load_templates():
    templates = {}
    if not os.path.isdir(TEMPLATES_DIR):
        return templates
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if fn.lower().endswith(".json"):
            try:
                with open(os.path.join(TEMPLATES_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                    templates[d.get("name", fn)] = d
            except Exception as e:
                print("Template load error:", fn, e)
    return templates

# ---------------- database ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    product_code TEXT UNIQUE,
                    name TEXT,
                    price_per_lb REAL
                 )""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if "tare" not in cols:
        c.execute("ALTER TABLE products ADD COLUMN tare REAL DEFAULT 0.0")
    if "plu_upc" not in cols:
        c.execute("ALTER TABLE products ADD COLUMN plu_upc TEXT")
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO products (product_code,name,price_per_lb,tare,plu_upc) VALUES (?,?,?,?,?)",
                  ("12345", "Chicken Breast", 2.99, 0.05, "12345"))
    conn.commit()
    conn.close()

# ---------------- settings ----------------
def load_settings():
    defaults = {
        "scale_port": "Simulate",
        "scale_baud": 9600,
        "printer_port": "COM1",
        "printer_baud": 38400,
        "last_template": None,
        "templates_dir": None,
        "last_product": None
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                defaults.update(data)
        except Exception:
            pass
    return defaults

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        print("Failed to save settings:", e)

# ---------------- UPC helpers ----------------
def upc_check_digit(digits11):
    if len(digits11) != 11:
        raise ValueError("Expected 11 digits")
    s_odd = sum(int(digits11[i]) for i in range(0, 11, 2))
    s_even = sum(int(digits11[i]) for i in range(1, 11, 2))
    total = s_odd * 3 + s_even
    check = (10 - (total % 10)) % 10
    return str(check)

def make_price_embedded_upc(plu5, price_cents):
    p = str(plu5).zfill(5)
    p5 = str(int(price_cents)).zfill(5)
    core = "2" + p + p5
    return core + upc_check_digit(core)

# ---------------- Scale Interface ----------------
class ScaleInterface:
    def __init__(self, port="Simulate", baud=9600):
        self.port = port
        self.baud = int(baud)
        self.simulate = (port == "Simulate") or (serial is None)
        self._running = False
        self._thread = None
        self._last_trigger = 0.0
        self.on_print = None

    def start(self):
        if self._running:
            return
        self._running = True
        self.simulate = (self.port == "Simulate") or (serial is None)
        if self.simulate:
            self._thread = threading.Thread(target=self._simulate_loop, daemon=True)
        else:
            self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None

    def _simulate_loop(self):
        import random
        while self._running:
            time.sleep(5)
            w = round(random.uniform(0.5, 8.0), 3)
            self._trigger(w)

    def _read_loop(self):
        if serial is None:
            print("pyserial not available, cannot open", self.port)
            return
        try:
            with serial.Serial(self.port, self.baud, timeout=1) as ser:
                while self._running:
                    raw = ser.readline().decode(errors="ignore").strip()
                    if not raw:
                        continue
                    m = re.search(r"(\d+\.\d+)", raw)
                    if m:
                        try:
                            w = float(m.group(1))
                        except Exception:
                            continue
                        self._trigger(w)
        except Exception as e:
            print("Serial read error:", e)

    def _trigger(self, weight):
        now = time.time()
        if now - self._last_trigger < 1.2:
            return
        self._last_trigger = now
        if self.on_print:
            try:
                self.on_print(weight)
            except Exception:
                pass

    def read_once(self, timeout=1.0):
        if self.simulate:
            import random
            return round(random.uniform(0.5, 8.0), 3)
        if serial is None:
            raise RuntimeError("pyserial not installed")
        try:
            with serial.Serial(self.port, self.baud, timeout=timeout) as ser:
                raw = ser.readline().decode(errors="ignore").strip()
                m = re.search(r"(\d+\.\d+)", raw)
                if m:
                    return float(m.group(1))
        except Exception as e:
            print("Read once error:", e)
        return 0.0

# ---------------- Advanced Options ----------------
class AdvancedOptions(tk.Toplevel):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("Advanced Options")
        self.geometry("600x200")
        self.build_ui()

    def build_ui(self):
        frm = ttk.Frame(self, padding=10); frm.pack(fill="both", expand=True)
        # Scale row
        ttk.Label(frm, text="Scale Port:").grid(column=0,row=0,sticky="w")
        ports = ["Simulate"] + self.app.enumerate_ports()
        ttk.Combobox(frm, textvariable=self.app.scale_port, values=ports, state="readonly").grid(column=1,row=0,sticky="w")
        ttk.Label(frm, text="Baud:").grid(column=2,row=0,sticky="w")
        ttk.Combobox(frm, textvariable=self.app.scale_baud, values=[9600,19200,38400,57600,115200], state="readonly").grid(column=3,row=0,sticky="w")
        ttk.Button(frm, text="Test Scale", command=self.app.test_scale).grid(column=4,row=0,sticky="w")
        self.app.listen_btn = ttk.Button(frm, text="Start Listening", command=self.app.toggle_listen)
        self.app.listen_btn.grid(column=5,row=0,sticky="w")
        # Printer row
        ttk.Label(frm, text="Printer Port:").grid(column=0,row=1,sticky="w")
        ttk.Combobox(frm, textvariable=self.app.printer_port, values=self.app.enumerate_ports(), state="readonly").grid(column=1,row=1,sticky="w")
        ttk.Label(frm, text="Baud:").grid(column=2,row=1,sticky="w")
        ttk.Entry(frm, textvariable=self.app.printer_baud, width=8).grid(column=3,row=1,sticky="w")
        ttk.Button(frm, text="Test Printer", command=self.app.test_printer).grid(column=4,row=1,sticky="w")

# ---------------- Main GUI ----------------
class App:
    def __init__(self, root):
        self.root = root; root.title("L+D Turkey Labeler")
        ensure_templates(); init_db()
        self.settings = load_settings()
        td = self.settings.get("templates_dir")
        if td:
            global TEMPLATES_DIR; TEMPLATES_DIR = td
        self.templates = load_templates()
        if not self.settings.get("last_template") and self.templates:
            self.settings["last_template"] = list(self.templates.keys())[0]
        self.conn = sqlite3.connect(DB_FILE)
        # variables
        self.scale_port = tk.StringVar(value=self.settings.get("scale_port","Simulate"))
        self.scale_baud = tk.IntVar(value=self.settings.get("scale_baud",9600))
        self.printer_port = tk.StringVar(value=self.settings.get("printer_port","COM1"))
        self.printer_baud = tk.IntVar(value=self.settings.get("printer_baud",38400))
        self.template_var = tk.StringVar(value=self.settings.get("last_template"))
        self.scale = ScaleInterface(port=self.scale_port.get(), baud=self.scale_baud.get())
        self.scale.on_print = self.handle_scale_print
        self.build_ui()

    def build_ui(self):
        frm = ttk.Frame(self.root, padding=10); frm.grid()
        # Template row
        ttk.Label(frm, text="Template:").grid(column=0,row=0,sticky="w")
        self.template_cb = ttk.Combobox(frm, textvariable=self.template_var, values=list(self.templates.keys()), state="readonly"); self.template_cb.grid(column=1,row=0,sticky="w")
        ttk.Button(frm, text="Browse Templates Folder", command=self.browse_templates).grid(column=2,row=0,sticky="w")
        ttk.Button(frm, text="Refresh Templates", command=self.refresh_template_list).grid(column=3,row=0,sticky="w")
        ttk.Button(frm, text="Advanced Options...", command=self.open_advanced).grid(column=4,row=0,sticky="w")

        # Product row
        ttk.Label(frm, text="Product:").grid(column=0,row=1,sticky="w")
        self.product_combo = ttk.Combobox(frm, values=self.load_product_list()); self.product_combo.grid(column=1,row=1,sticky="w")
        ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2,row=1,sticky="w")

        # Weight and fields
        ttk.Label(frm, text="Weight (gross lb):").grid(column=0,row=2,sticky="w")
        self.weight_var = tk.DoubleVar(value=0.0); ttk.Entry(frm, textvariable=self.weight_var).grid(column=1,row=2,sticky="w")
        ttk.Button(frm, text="Read Weight", command=self.manual_read).grid(column=2,row=2,sticky="w")
        ttk.Label(frm, text="Sell-by (YYYY-MM-DD):").grid(column=0,row=3,sticky="w"); self.sellby_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d")); ttk.Entry(frm, textvariable=self.sellby_var).grid(column=1,row=3,sticky="w")
        ttk.Label(frm, text="Lot:").grid(column=0,row=4,sticky="w"); self.lot_var = tk.StringVar(value=""); ttk.Entry(frm, textvariable=self.lot_var).grid(column=1,row=4,sticky="w")

        # Actions
        ttk.Button(frm, text="Preview (PDF)", command=self.preview).grid(column=0,row=5,sticky="w")
        ttk.Button(frm, text="Print (send to printer)", command=self.print_to_printer).grid(column=1,row=5,sticky="w")

        self.status = tk.StringVar(value="Idle"); ttk.Label(frm, textvariable=self.status).grid(column=0,row=6,columnspan=4,sticky="w")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- existing methods reused ---
    def enumerate_ports(self):
        ports = []
        if list_ports is not None:
            try:
                for p in list_ports.comports():
                    ports.append(p.device)
            except Exception:
                pass
        for i in range(1, 21):
            n = f"COM{i}"
            if n not in ports:
                ports.append(n)
        return ports

    def refresh_template_list(self):
        self.templates = load_templates()
        vals = list(self.templates.keys())
        self.template_cb["values"] = vals
        if vals and (self.template_var.get() not in vals):
            self.template_var.set(vals[0])

    def browse_templates(self):
        folder = filedialog.askdirectory(title="Select Templates Folder")
        if folder:
            global TEMPLATES_DIR
            TEMPLATES_DIR = folder
            self.settings["templates_dir"] = folder
            save_settings(self.settings)
            self.refresh_template_list()

    def load_product_list(self):
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products"); rows = c.fetchall(); conn.close()
        return [f"{r[0]} - {r[1]} (${r[2]:.2f}/lb, tare {r[3]:.3f}, PLU {r[4] or ''})" for r in rows]

    def open_product
