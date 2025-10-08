#!/usr/bin/env python3
"""
LD Turkey Labeler - Combined final script

Features:
- Templates loading (browse or bundled)
- Product DB with tare and PLU/UPC (migrates columns if missing)
- Scale interface (simulate or serial) with Start/Stop listening
- Separate Scale and Printer COM + baud selection, Test buttons
- PDF preview (ReportLab) and raw DPL printing to Datamax via serial
- Settings saved to settings.json
"""

import os
import sys
import json
import sqlite3
import threading
import time
import re
from datetime import datetime

# optional serial/list_ports
try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

# reportlab for PDF & barcode
try:
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.graphics.barcode import createBarcodeDrawing
    from reportlab.lib.utils import ImageReader
except Exception:
    createBarcodeDrawing = None
    ImageReader = None

# tkinter GUI
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception:
    print("Tkinter required. On Debian/Ubuntu: sudo apt-get install python3-tk")
    sys.exit(1)

# ---------------- helpers ----------------
def resource_path(rel_path):
    """Return absolute path for a resource (works both in dev and PyInstaller onefile)."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel_path)

# ---------------- config ----------------
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
        try:
            with open(sample_path, "w", encoding="utf-8") as f:
                json.dump(sample, f, indent=2)
        except Exception:
            pass

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
    # base table
    c.execute("""CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    product_code TEXT UNIQUE,
                    name TEXT,
                    price_per_lb REAL
                 )""")
    # migrations
    cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if "tare" not in cols:
        try:
            c.execute("ALTER TABLE products ADD COLUMN tare REAL DEFAULT 0.0")
        except Exception:
            pass
    if "plu_upc" not in cols:
        try:
            c.execute("ALTER TABLE products ADD COLUMN plu_upc TEXT")
        except Exception:
            pass
    # seed
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

# ---------------- PDF generation ----------------
def generate_label_pdf(output_path, template, content):
    if createBarcodeDrawing is None:
        raise RuntimeError("reportlab not installed")
    width_in, height_in = template.get("size_in", [LABEL_WIDTH_IN, LABEL_HEIGHT_IN])
    w = width_in * inch
    h = height_in * inch
    c = canvas.Canvas(output_path, pagesize=(w, h))
    font = template.get("font", "Helvetica")
    for fld in template.get("fields", []):
        name = fld.get("name")
        x = fld.get("x", 0) * inch
        y = fld.get("y", 0) * inch
        size = fld.get("size", 8)
        if name == "barcode":
            code = content.get("upc", "")
            try:
                drawing = createBarcodeDrawing("UPCA", value=code, barHeight=fld.get("height", 0.3) * inch, humanReadable=True)
                drawing.drawOn(c, x, y)
            except Exception:
                c.setFont(font, 6)
                c.drawString(x, y, "UPC:" + code)
            continue
        if name == "logo":
            if ImageReader is None:
                c.setFont(font, 6)
                c.drawString(x, y, "[Image not supported - reportlab missing]")
                continue
            img_path = fld.get("path") or fld.get("file") or "logo.png"
            candidate = img_path if os.path.isabs(img_path) else os.path.join(TEMPLATES_DIR, img_path)
            if not os.path.exists(candidate):
                candidate = resource_path(img_path)
            if os.path.exists(candidate):
                try:
                    iw = fld.get("width", 0.5) * inch
                    ih = fld.get("height", 0.5) * inch
                    c.drawImage(ImageReader(candidate), x, y, width=iw, height=ih, preserveAspectRatio=True, mask="auto")
                except Exception:
                    c.setFont(font, 6)
                    c.drawString(x, y, "[Image error]")
            else:
                c.setFont(font, 6)
                c.drawString(x, y, "[Image not found]")
            continue
        # text fields
        text = ""
        if name == "product_name":
            text = content.get("product_name", "")
        elif name == "weight":
            text = f"Weight: {content.get('weight', 0):.3f} lb"
        elif name == "price_per_lb":
            text = f"{content.get('price_per_lb', 0):.2f} /lb"
        elif name == "total_price":
            text = f"Total: ${content.get('total_price', 0):.2f}"
        elif name == "sell_by":
            text = f"Sell by: {content.get('sell_by', '')}"
        elif name == "lot":
            text = f"Lot: {content.get('lot', '')}"
        else:
            text = str(content.get(name, ""))
        try:
            c.setFont(font, size)
        except Exception:
            c.setFont("Helvetica", size)
        c.drawString(x, y, text)
    c.showPage()
    c.save()

# ---------------- DPL generation ----------------
def generate_dpl_label(content):
    # Very simple DPL layout — tune for your Datamax model/firmware
    lines = []
    lines.append("\x02L")  # start label
    lines.append("D11")    # density
    lines.append(f"191100000300093{content.get('product_name','')}")
    lines.append(f"191100000500093Weight: {content.get('weight',0):.3f} lb")
    lines.append(f"191100000700093Total: ${content.get('total_price',0):.2f}")
    lines.append(f"191100000900093Lot: {content.get('lot','')}")
    # placeholder barcode line — adjust to correct DPL for UPC-A if needed
    lines.append(f"1e4206000300123{content.get('upc','')}")
    lines.append("E")
    return ("\n".join(lines)).encode("ascii", errors="replace")

# ---------------- Product Manager ----------------
class ProductManager(tk.Toplevel):
    def __init__(self, parent, refresh_cb):
        super().__init__(parent)
        self.title("Product Manager")
        self.geometry("700x420")
        self.refresh_cb = refresh_cb
        self.build_ui()
        self.load()

    def build_ui(self):
        frm = ttk.Frame(self, padding=8); frm.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frm, columns=("code","name","price","tare","plu"), show="headings")
        for col,txt in (("code","Code"),("name","Name"),("price","Price/lb"),("tare","Tare"),("plu","PLU")):
            self.tree.heading(col, text=txt); self.tree.column(col, width=140)
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(frm); btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Add", command=self.add).pack(side="left", padx=4)
        ttk.Button(btns, text="Edit", command=self.edit).pack(side="left", padx=4)
        ttk.Button(btns, text="Delete", command=self.delete).pack(side="left", padx=4)

    def load(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products")
        for r in c.fetchall():
            self.tree.insert("", "end", values=(r[0], r[1], f"{r[2]:.2f}", f"{r[3]:.3f}", r[4] or ""))
        conn.close()

    def add(self): self.editor()
    def edit(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]; self.editor(vals)
    def delete(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        if messagebox.askyesno("Delete", f"Delete {vals[0]}?"):
            conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("DELETE FROM products WHERE product_code=?", (vals[0],)); conn.commit(); conn.close(); self.load(); self.refresh_cb()

    def editor(self, vals=None):
        w = tk.Toplevel(self); w.title("Edit Product"); w.geometry("420x220")
        labels = ["Product Code","Name","Price per lb","Tare (lb)","PLU/UPC"]
        vars = []
        defaults = vals if vals else ("","", "0.00", "0.000", "")
        for i, lbl in enumerate(labels):
            ttk.Label(w, text=lbl).grid(column=0, row=i, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=defaults[i])
            ttk.Entry(w, textvariable=var).grid(column=1, row=i, sticky="ew", padx=8, pady=4)
            vars.append(var)
        def save():
            code = vars[0].get().strip(); name = vars[1].get().strip()
            try: price = float(vars[2].get())
            except Exception: price = 0.0
            try: tare = float(vars[3].get())
            except Exception: tare = 0.0
            plu = vars[4].get().strip()
            if not code or not name:
                messagebox.showerror("Error","Code and Name required"); return
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO products (product_code,name,price_per_lb,tare,plu_upc) VALUES (?,?,?,?,?)",(code, name, price, tare, plu))
            conn.commit(); conn.close(); w.destroy(); self.load(); self.refresh_cb()
        ttk.Button(w, text="Save", command=save).grid(column=0, row=len(labels), columnspan=2, sticky="ew", padx=8, pady=8)

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
        # Scale row
        ttk.Label(frm, text="Scale Port:").grid(column=0,row=0,sticky="w")
        ports = ["Simulate"] + self.enumerate_ports()
        self.scale_port_cb = ttk.Combobox(frm, textvariable=self.scale_port, values=ports, state="readonly"); self.scale_port_cb.grid(column=1,row=0,sticky="w")
        ttk.Label(frm, text="Baud:").grid(column=2,row=0,sticky="w"); ttk.Combobox(frm, textvariable=self.scale_baud, values=[9600,19200,38400,57600,115200], state="readonly").grid(column=3,row=0,sticky="w")
        ttk.Button(frm, text="Test Scale", command=self.test_scale).grid(column=4,row=0,sticky="w")
        self.listen_btn = ttk.Button(frm, text="Start Listening", command=self.toggle_listen); self.listen_btn.grid(column=5,row=0,sticky="w")

        # Printer row
        ttk.Label(frm, text="Printer Port:").grid(column=0,row=1,sticky="w")
        self.printer_port_cb = ttk.Combobox(frm, textvariable=self.printer_port, values=self.enumerate_ports(), state="readonly"); self.printer_port_cb.grid(column=1,row=1,sticky="w")
        ttk.Label(frm, text="Baud:").grid(column=2,row=1,sticky="w"); ttk.Entry(frm, textvariable=self.printer_baud, width=8).grid(column=3,row=1,sticky="w")
        ttk.Button(frm, text="Test Printer", command=self.test_printer).grid(column=4,row=1,sticky="w")

        # Template row
        ttk.Label(frm, text="Template:").grid(column=0,row=2,sticky="w")
        self.template_cb = ttk.Combobox(frm, textvariable=self.template_var, values=list(self.templates.keys()), state="readonly"); self.template_cb.grid(column=1,row=2,sticky="w")
        ttk.Button(frm, text="Browse Templates Folder", command=self.browse_templates).grid(column=2,row=2,sticky="w")
        ttk.Button(frm, text="Refresh Templates", command=self.refresh_template_list).grid(column=3,row=2,sticky="w")

        # Product row
        ttk.Label(frm, text="Product:").grid(column=0,row=3,sticky="w")
        self.product_combo = ttk.Combobox(frm, values=self.load_product_list()); self.product_combo.grid(column=1,row=3,sticky="w")
        ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2,row=3,sticky="w")

        # Weight and fields
        ttk.Label(frm, text="Weight (gross lb):").grid(column=0,row=4,sticky="w")
        self.weight_var = tk.DoubleVar(value=0.0); ttk.Entry(frm, textvariable=self.weight_var).grid(column=1,row=4,sticky="w")
        ttk.Button(frm, text="Read Weight", command=self.manual_read).grid(column=2,row=4,sticky="w")
        ttk.Label(frm, text="Sell-by (YYYY-MM-DD):").grid(column=0,row=5,sticky="w"); self.sellby_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d")); ttk.Entry(frm, textvariable=self.sellby_var).grid(column=1,row=5,sticky="w")
        ttk.Label(frm, text="Lot:").grid(column=0,row=6,sticky="w"); self.lot_var = tk.StringVar(value=""); ttk.Entry(frm, textvariable=self.lot_var).grid(column=1,row=6,sticky="w")

        # Actions
        ttk.Button(frm, text="Preview (PDF)", command=self.preview).grid(column=0,row=7,sticky="w")
        ttk.Button(frm, text="Print (send to printer)", command=self.print_to_printer).grid(column=1,row=7,sticky="w")

        self.status = tk.StringVar(value="Idle"); ttk.Label(frm, textvariable=self.status).grid(column=0,row=8,columnspan=4,sticky="w")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # helpers
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

    def open_product_manager(self): ProductManager(self.root, refresh_cb=self.reload_products)
    def reload_products(self): self.product_combo["values"] = self.load_product_list()

    def parse_product(self):
        v = self.product_combo.get()
        if not v:
            messagebox.showerror("Error", "Select product"); return None
        code = v.split(" - ")[0].strip()
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products WHERE product_code=?", (code,)); row = c.fetchone(); conn.close()
        if not row:
            messagebox.showerror("Error", "Product not found"); return None
        return {"product_code": row[0], "name": row[1], "price_per_lb": row[2], "tare": row[3] or 0.0, "plu_upc": row[4]}

    # tests
    def test_scale(self):
        port = self.scale_port.get(); baud = int(self.scale_baud.get())
        if port == "Simulate" or serial is None:
            messagebox.showinfo("Scale Test", "Simulation mode - no device checked.")
            return
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                ser.write(b"\r\n")
                resp = ser.readline().decode(errors="ignore").strip()
            messagebox.showinfo("Scale Test", f"Scale responded: {resp}")
        except Exception as e:
            messagebox.showerror("Scale Test Failed", str(e))

    def test_printer(self):
        port = self.printer_port.get(); baud = int(self.printer_baud.get())
        if serial is None:
            messagebox.showerror("Printer Test", "pyserial not installed.")
            return
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                dpl = b"\x02L\nD11\n191100000300093TEST PRINT\nE\n"
                ser.write(dpl)
            messagebox.showinfo("Printer Test", "Test label sent.")
        except Exception as e:
            messagebox.showerror("Printer Test Failed", str(e))

    # weight / printing
    def manual_read(self):
        try:
            self.scale.port = self.scale_port.get()
            self.scale.baud = int(self.scale_baud.get())
            w = self.scale.read_once()
        except Exception as e:
            messagebox.showerror("Error", f"Read failed: {e}"); return
        self.weight_var.set(w); messagebox.showinfo("Weight Read", f"Gross: {w:.3f} lb")

    def generate_content(self, weight):
        prod = self.parse_product()
        if not prod: return None
        net = max(0.0, weight - float(prod["tare"] or 0.0))
        total = round(net * prod["price_per_lb"] + 1e-9, 2)
        cents = int(round(total * 100))
        upc_src = prod["plu_upc"] or prod["product_code"]
        upc_code = make_price_embedded_upc(upc_src, cents)
        return {"product_name": prod["name"], "weight": net, "price_per_lb": prod["price_per_lb"], "total_price": total, "sell_by": self.sellby_var.get(), "lot": self.lot_var.get(), "upc": upc_code}

    def preview(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error", "Invalid weight"); return
        content = self.generate_content(w)
        if not content: return
        tplname = self.template_var.get()
        tpl = self.templates.get(tplname) if tplname else None
        if not tpl: messagebox.showerror("Error", "Select template"); return
        out = os.path.abspath("preview_label.pdf")
        try:
            generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("Error", f"PDF failed: {e}"); return
        messagebox.showinfo("Preview", f"PDF: {out}")
        if sys.platform.startswith("win"): os.startfile(out)

    def print_to_printer(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error", "Invalid weight"); return
        content = self.generate_content(w)
        if not content: return
        tplname = self.template_var.get(); tpl = self.templates.get(tplname) if tplname else None
        out = os.path.abspath(f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        try:
            if tpl: generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("Error", f"PDF failed: {e}")
        port = self.printer_port.get(); baud = int(self.printer_baud.get())
        if serial is None:
            messagebox.showerror("Printer Error", "pyserial not installed. Cannot send to COM port."); return
        try:
            dpl = generate_dpl_label(content)
            with serial.Serial(port, baud, timeout=2) as ser:
                ser.write(dpl)
            messagebox.showinfo("Printer", f"Label sent to {port}")
        except Exception as e:
            messagebox.showerror("Printer Error", str(e))

    def handle_scale_print(self, weight):
        def do_auto():
            self.weight_var.set(weight)
            content = self.generate_content(weight)
            if not content:
                self.status.set("Auto skipped (no product)"); return
            tplname = self.template_var.get(); tpl = self.templates.get(tplname) if tplname else None
            out = os.path.abspath(f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            try:
                if tpl: generate_label_pdf(out, tpl, content)
            except Exception as e:
                self.status.set(f"Auto PDF failed: {e}")
            port = self.printer_port.get(); baud = int(self.printer_baud.get())
            if serial is None:
                self.status.set("Auto skipped (pyserial missing)"); return
            try:
                dpl = generate_dpl_label(content)
                with serial.Serial(port, baud, timeout=2) as ser:
                    ser.write(dpl)
                self.status.set(f"Auto-sent to {port}")
            except Exception as e:
                self.status.set(f"Auto send failed: {e}")
        try: self.root.after(0, do_auto)
        except Exception: pass

    def toggle_listen(self):
        if getattr(self.scale, "_running", False):
            self.scale.stop(); self.listen_btn.config(text="Start Listening"); self.status.set("Idle")
        else:
            # update scale settings
            self.scale.port = self.scale_port.get(); self.scale.baud = int(self.scale_baud.get())
            self.scale.simulate = (self.scale.port == "Simulate") or (serial is None)
            try:
                self.scale.start(); self.listen_btn.config(text="Stop Listening"); self.status.set(f"Listening on {self.scale.port}@{self.scale.baud}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start listener: {e}")

    def on_close(self):
        try: self.scale.stop()
        except Exception: pass
        try: self.conn.close()
        except Exception: pass
        # save settings
        self.settings["scale_port"] = self.scale_port.get()
        self.settings["scale_baud"] = int(self.scale_baud.get())
        self.settings["printer_port"] = self.printer_port.get()
        self.settings["printer_baud"] = int(self.printer_baud.get())
        self.settings["last_template"] = self.template_var.get()
        save_settings(self.settings)
        self.root.destroy()

# ---------------- main ----------------
def main():
    ensure_templates(); init_db()
    root = tk.Tk(); app = App(root); root.mainloop()

if __name__ == "__main__":
    main()
