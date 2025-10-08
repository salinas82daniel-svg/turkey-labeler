#!/usr/bin/env python3
"""
ver.py - L+D Turkey Labeler (full version)
Features:
- Templates: JSON (layout) and PRN (raw printer commands with placeholders)
- Product DB with tare and PLU/UPC (migrates when needed)
- Scale interface (simulate or serial)
- Options window for Scale & Printer port/baud and template selection
- Test Scale + Test Printer (custom PRN editor)
- PDF preview when JSON template is selected; disabled for PRN
- Print: JSON -> PDF + (optional) send DPL; PRN -> replace placeholders and send raw to COM
Settings persist in settings.json
"""

import os, sys, json, sqlite3, threading, time, re
from datetime import datetime

# serial support
try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

# reportlab support for PDF preview/generation
try:
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.graphics.barcode import createBarcodeDrawing
    from reportlab.lib.utils import ImageReader
except Exception:
    createBarcodeDrawing = None
    ImageReader = None

# tkinter
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except Exception:
    print("tkinter is required. On Debian/Ubuntu: sudo apt-get install python3-tk")
    sys.exit(1)

# ---------------- paths & defaults ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DB_FILE = os.path.join(BASE_DIR, "products.db")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# ---------------- helpers ----------------
def ensure_templates():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    sample_json = {
        "name": "default_2x2",
        "size_in": [2.0, 2.0],
        "font": "Helvetica",
        "fields": [
            {"name": "product_name", "x": 0.1, "y": 1.35, "size": 10},
            {"name": "weight", "x": 0.1, "y": 1.05, "size": 9},
            {"name": "price_per_lb", "x": 0.1, "y": 0.85, "size": 9},
            {"name": "total_price", "x": 0.1, "y": 0.65, "size": 12},
            {"name": "sell_by", "x": 0.1, "y": 0.45, "size": 8},
            {"name": "lot", "x": 0.1, "y": 0.25, "size": 8},
            {"name": "barcode", "x": 0.1, "y": 0.02, "width": 1.8, "height": 0.45}
        ]
    }
    default_json_path = os.path.join(TEMPLATES_DIR, "default_2x2.json")
    if not os.path.exists(default_json_path):
        try:
            with open(default_json_path, "w", encoding="utf-8") as f:
                json.dump(sample_json, f, indent=2)
        except Exception:
            pass
    # sample PRN
    sample_prn = '<STX>L<CR>\nA50,30,0,3,1,1,N,"{product_name}"<CR>\nA50,70,0,3,1,1,N,"{weight} lb"<CR>\nA50,110,0,3,1,1,N,"${total_price}"<CR>\nB50,150,0,E30,2,2,100,N,"{upc}"<CR>\nE<CR>\n'
    prn_path = os.path.join(TEMPLATES_DIR, "sample_label.prn")
    if not os.path.exists(prn_path):
        try:
            with open(prn_path, "w", encoding="utf-8") as f:
                f.write(sample_prn)
        except Exception:
            pass

def list_template_files():
    ensure_templates()
    files = []
    for fn in sorted(os.listdir(TEMPLATES_DIR)):
        if fn.lower().endswith(".json") or fn.lower().endswith(".prn"):
            files.append(fn)
    return files

# ---------------- db ----------------
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
        try: c.execute("ALTER TABLE products ADD COLUMN tare REAL DEFAULT 0.0")
        except Exception: pass
    if "plu_upc" not in cols:
        try: c.execute("ALTER TABLE products ADD COLUMN plu_upc TEXT")
        except Exception: pass
    c.execute("SELECT COUNT(*) FROM products")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO products (product_code,name,price_per_lb,tare,plu_upc) VALUES (?,?,?,?,?)",
                  ("12345","Chicken Breast",2.99,0.05,"12345"))
    conn.commit(); conn.close()

# ---------------- settings ----------------
def load_settings():
    defaults = {
        "scale_port": "Simulate",
        "scale_baud": 9600,
        "printer_port": "COM1",
        "printer_baud": 38400,
        "last_template": None,
        "custom_prn": None
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f); defaults.update(data)
        except Exception:
            pass
    return defaults

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        print("Failed saving settings:", e)

# ---------------- UPC helpers ----------------
def upc_check_digit(digits11):
    if len(digits11) != 11: raise ValueError("Expected 11 digits")
    s_odd = sum(int(digits11[i]) for i in range(0,11,2))
    s_even = sum(int(digits11[i]) for i in range(1,11,2))
    total = s_odd*3 + s_even
    check = (10 - (total % 10)) % 10
    return str(check)

def make_price_embedded_upc(plu5, price_cents):
    p = str(plu5).zfill(5); p5 = str(int(price_cents)).zfill(5)
    core = "2" + p + p5; return core + upc_check_digit(core)

# ---------------- scale interface ----------------
class ScaleInterface:
    def __init__(self, port="Simulate", baud=9600):
        self.port = port; self.baud = int(baud)
        self.simulate = (port == "Simulate") or (serial is None)
        self._running = False; self._thread = None; self._last_trigger = 0; self.on_print = None

    def start(self):
        if self._running: return
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
            self._thread.join(timeout=0.5); self._thread = None

    def _simulate_loop(self):
        import random
        while self._running:
            time.sleep(5)
            self._trigger(round(random.uniform(0.5,8.0),3))

    def _read_loop(self):
        if serial is None:
            print("pyserial not available"); return
        try:
            with serial.Serial(self.port, self.baud, timeout=1) as ser:
                while self._running:
                    raw = ser.readline().decode(errors="ignore").strip()
                    if not raw: continue
                    m = re.search(r"(\d+\.\d+)", raw)
                    if m:
                        try: w = float(m.group(1))
                        except: continue
                        self._trigger(w)
        except Exception as e:
            print("Scale read error:", e)

    def _trigger(self, w):
        now = time.time()
        if now - self._last_trigger < 1.2: return
        self._last_trigger = now
        if self.on_print:
            try: self.on_print(w)
            except Exception: pass

    def read_once(self, timeout=1.0):
        if self.simulate:
            import random; return round(random.uniform(0.5,8.0),3)
        if serial is None: raise RuntimeError("pyserial not installed")
        try:
            with serial.Serial(self.port, self.baud, timeout=timeout) as ser:
                raw = ser.readline().decode(errors="ignore").strip()
                m = re.search(r"(\d+\.\d+)", raw)
                if m: return float(m.group(1))
        except Exception as e:
            print("Read once error:", e)
        return 0.0

# ---------------- PDF generator ----------------
def generate_label_pdf(path, template, content):
    if createBarcodeDrawing is None:
        raise RuntimeError("reportlab not installed")
    size_in = template.get("size_in",[2.0,2.0])
    w = size_in[0]*inch; h = size_in[1]*inch
    c = canvas.Canvas(path, pagesize=(w,h))
    font = template.get("font","Helvetica")
    for fld in template.get("fields",[]):
        name = fld.get("name"); x = fld.get("x",0)*inch; y = fld.get("y",0)*inch; size = fld.get("size",8)
        if name == "barcode":
            code = content.get("upc","")
            try:
                drawing = createBarcodeDrawing("UPCA", value=code, barHeight=fld.get("height",0.3)*inch, humanReadable=True)
                drawing.drawOn(c,x,y)
            except Exception:
                c.setFont(font,6); c.drawString(x,y,"UPC:"+code)
            continue
        # text
        text = ""
        if name=="product_name": text = content.get("product_name","")
        elif name=="weight": text = f"Weight: {content.get('weight',0):.3f} lb"
        elif name=="price_per_lb": text = f"{content.get('price_per_lb',0):.2f} /lb"
        elif name=="total_price": text = f"Total: ${content.get('total_price',0):.2f}"
        elif name=="sell_by": text = f"Sell by: {content.get('sell_by','')}"
        elif name=="lot": text = f"Lot: {content.get('lot','')}"
        else: text = str(content.get(name,""))
        try: c.setFont(font,size)
        except: c.setFont("Helvetica", size)
        c.drawString(x,y,text)
    c.showPage(); c.save()

# ---------------- PRN support ----------------
def load_prn(path):
    try:
        with open(path, "r", encoding="utf-8") as f: return f.read()
    except Exception as e:
        print("Load PRN failed:", e); return None

def render_prn(prn_text, content):
    out = prn_text
    # placeholders upper-case
    for k,v in content.items():
        out = out.replace("{" + k.upper() + "}", str(v))
        out = out.replace("{" + k + "}", str(v))
    return out

def send_prn_to_printer(port, baud, prn_payload):
    if serial is None:
        raise RuntimeError("pyserial not installed")
    with serial.Serial(port, int(baud), timeout=2) as ser:
        # ensure bytes
        if isinstance(prn_payload, str): prn_payload = prn_payload.encode("utf-8")
        ser.write(prn_payload)

# ---------------- datamax generator (from JSON template) ----------------
def inches_to_dots(v_in, dpi=203):
    return int(round(v_in * dpi))

def generate_datamax_from_template(template, content, dpi=203):
    # Basic conversion: place fields by absolute dot coordinates.
    lines = ["N"]
    width = inches_to_dots(template.get("size_in",[2.0,2.0])[0], dpi)
    height = inches_to_dots(template.get("size_in",[2.0,2.0])[1], dpi)
    lines.append(f"q{width}")
    lines.append(f"Q{height},24")
    for fld in template.get("fields",[]):
        name = fld.get("name"); x = inches_to_dots(fld.get("x",0),dpi); y = inches_to_dots(fld.get("y",0),dpi)
        if name=="barcode":
            code = content.get("upc","")
            h = inches_to_dots(fld.get("height",0.3), dpi)
            lines.append(f'B{x},{y},0,E30,2,2,{h},N,"{code}"')
        else:
            text = ""
            if name=="product_name": text = content.get("product_name","")
            elif name=="weight": text = f"Weight: {content.get('weight',0):.3f} lb"
            elif name=="price_per_lb": text = f"{content.get('price_per_lb',0):.2f} /lb"
            elif name=="total_price": text = f"Total: ${content.get('total_price',0):.2f}"
            elif name=="sell_by": text = f"Sell by: {content.get('sell_by','')}"
            elif name=="lot": text = f"Lot: {content.get('lot','')}"
            else: text = str(content.get(name,""))
            # A command: A{x},{y},rotation,font,horz,vert,invert,"text"
            lines.append(f'A{x},{y},0,3,1,1,N,"{text}"')
    lines.append("P1")
    return "\n".join(lines)

# ---------------- Product Manager ----------------
class ProductManager(tk.Toplevel):
    def __init__(self, parent, refresh_cb):
        super().__init__(parent)
        self.title("Product Manager"); self.geometry("700x420")
        self.refresh_cb = refresh_cb; self.build_ui(); self.load()

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
        sel = self.tree.selection(); 
        if not sel: return
        vals = self.tree.item(sel[0])["values"]; self.editor(vals)
    def delete(self):
        sel = self.tree.selection(); 
        if not sel: return
        vals = self.tree.item(sel[0])["values"]
        if messagebox.askyesno("Delete", f"Delete {vals[0]}?"):
            conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("DELETE FROM products WHERE product_code=?", (vals[0],)); conn.commit(); conn.close(); self.load(); self.refresh_cb()

    def editor(self, vals=None):
        w = tk.Toplevel(self); w.title("Edit Product"); w.geometry("420x220")
        labels = ["Product Code","Name","Price per lb","Tare (lb)","PLU/UPC"]
        vars = []; defaults = vals if vals else ("","", "0.00", "0.000", "")
        for i, lbl in enumerate(labels):
            ttk.Label(w, text=lbl).grid(column=0, row=i, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=defaults[i]); ttk.Entry(w, textvariable=var).grid(column=1, row=i, sticky="ew", padx=8, pady=4); vars.append(var)
        def save():
            code = vars[0].get().strip(); name = vars[1].get().strip()
            try: price = float(vars[2].get())
            except: price = 0.0
            try: tare = float(vars[3].get())
            except: tare = 0.0
            plu = vars[4].get().strip()
            if not code or not name: messagebox.showerror("Error","Code and Name required"); return
            conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("INSERT OR REPLACE INTO products (product_code,name,price_per_lb,tare,plu_upc) VALUES (?,?,?,?,?)", (code, name, price, tare, plu)); conn.commit(); conn.close(); w.destroy(); self.load(); self.refresh_cb()
        ttk.Button(w, text="Save", command=save).grid(column=0, row=len(labels), columnspan=2, sticky="ew", padx=8, pady=8)

# ---------------- Options Window ----------------
class OptionsWindow(tk.Toplevel):
    def __init__(self, parent, settings, apply_cb):
        super().__init__(parent)
        self.title("Options"); self.geometry("620x420"); self.settings = settings; self.apply_cb = apply_cb
        self.build_ui()
    def build_ui(self):
        frm = ttk.Frame(self, padding=10); frm.grid(sticky="nw")
        # Scale settings
        ttk.Label(frm, text="Scale Port:").grid(column=0,row=0,sticky="w"); self.scale_port = tk.StringVar(value=self.settings.get("scale_port","Simulate"))
        ports = ["Simulate"] + [f"COM{i}" for i in range(1,21)]
        ttk.Combobox(frm, textvariable=self.scale_port, values=ports, state="readonly").grid(column=1,row=0,sticky="w")
        ttk.Label(frm, text="Scale Baud:").grid(column=2,row=0,sticky="w"); self.scale_baud = tk.IntVar(value=self.settings.get("scale_baud",9600))
        ttk.Combobox(frm, textvariable=self.scale_baud, values=[9600,19200,38400,57600,115200], state="readonly").grid(column=3,row=0,sticky="w")
        ttk.Button(frm, text="Test Scale", command=self.test_scale).grid(column=4,row=0,sticky="w")
        # Printer settings
        ttk.Label(frm, text="Printer Port:").grid(column=0,row=1,sticky="w"); self.printer_port = tk.StringVar(value=self.settings.get("printer_port","COM1"))
        ttk.Combobox(frm, textvariable=self.printer_port, values=[f"COM{i}" for i in range(1,21)], state="readonly").grid(column=1,row=1,sticky="w")
        ttk.Label(frm, text="Printer Baud:").grid(column=2,row=1,sticky="w"); self.printer_baud = tk.IntVar(value=self.settings.get("printer_baud",38400))
        ttk.Combobox(frm, textvariable=self.printer_baud, values=[9600,19200,38400,57600,115200], state="readonly").grid(column=3,row=1,sticky="w")
        ttk.Button(frm, text="Test Printer", command=self.test_printer).grid(column=4,row=1,sticky="w")
        # Template list preview and PRN editor
        ttk.Label(frm, text="Templates folder:").grid(column=0,row=2,sticky="w"); ttk.Label(frm, text=TEMPLATES_DIR).grid(column=1,row=2,sticky="w", columnspan=3)
        ttk.Button(frm, text="Open Templates Folder", command=self.open_templates_folder).grid(column=4,row=2,sticky="w")
        ttk.Label(frm, text="Custom Test PRN:").grid(column=0,row=3,sticky="nw"); self.custom_prn = tk.Text(frm, width=60, height=8)
        self.custom_prn.grid(column=1,row=3,columnspan=4,sticky="w")
        cp = self.settings.get("custom_prn") or "N\nq406\nQ406,24\nA20,20,0,2,1,1,N,\"TEST PRINT\"\nP1\n"
        self.custom_prn.delete("1.0","end"); self.custom_prn.insert("1.0", cp)
        ttk.Button(frm, text="Save", command=self.save).grid(column=1,row=5,sticky="w", pady=8)
    def open_templates_folder(self):
        os.startfile(TEMPLATES_DIR) if sys.platform.startswith("win") else messagebox.showinfo("Folder", TEMPLATES_DIR)
    def test_scale(self):
        port = self.scale_port.get(); baud = int(self.scale_baud.get())
        if port=="Simulate" or serial is None:
            messagebox.showinfo("Scale Test", "Simulation mode or pyserial missing")
            return
        try:
            with serial.Serial(port, baud, timeout=2) as ser:
                ser.write(b"\r\n"); resp = ser.readline().decode(errors="ignore").strip()
            messagebox.showinfo("Scale Test", f"Response: {resp}")
        except Exception as e:
            messagebox.showerror("Scale Test Failed", str(e))
    def test_printer(self):
        prn = self.custom_prn.get("1.0","end").strip()
        if serial is None:
            messagebox.showerror("Printer Test", "pyserial not installed")
            return
        try:
            with serial.Serial(self.printer_port.get(), int(self.printer_baud.get()), timeout=2) as ser:
                ser.write(prn.encode("utf-8"))
            messagebox.showinfo("Printer Test", "Custom PRN sent to printer")
        except Exception as e:
            messagebox.showerror("Printer Test Failed", str(e))
    def save(self):
        self.settings["scale_port"] = self.scale_port.get(); self.settings["scale_baud"] = int(self.scale_baud.get())
        self.settings["printer_port"] = self.printer_port.get(); self.settings["printer_baud"] = int(self.printer_baud.get())
        self.settings["custom_prn"] = self.custom_prn.get("1.0","end"); save_settings(self.settings); self.apply_cb(); self.destroy()

# ---------------- Main App ----------------
class App:
    def __init__(self, root):
        self.root = root; root.title("L+D Turkey Labeler")
        ensure_templates(); init_db(); self.settings = load_settings()
        self.templates = list_template_files()
        if not self.settings.get("last_template") and self.templates:
            self.settings["last_template"] = self.templates[0]
        # vars
        self.template_var = tk.StringVar(value=self.settings.get("last_template") or "")
        self.product_var = tk.StringVar(value="")
        self.weight_var = tk.DoubleVar(value=0.0)
        self.sellby_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.lot_var = tk.StringVar(value="")
        # scale
        self.scale = ScaleInterface(port=self.settings.get("scale_port","Simulate"), baud=self.settings.get("scale_baud",9600))
        self.scale.on_print = self.handle_scale_print
        # build ui
        self.build_ui()
        self.conn = sqlite3.connect(DB_FILE)
    def build_ui(self):
        frm = ttk.Frame(self.root, padding=10); frm.grid()
        ttk.Label(frm, text="Template:").grid(column=0,row=0,sticky="w")
        self.template_cb = ttk.Combobox(frm, textvariable=self.template_var, values=self.templates, state="readonly"); self.template_cb.grid(column=1,row=0,sticky="w")
        ttk.Button(frm, text="Options", command=self.open_options).grid(column=2,row=0,sticky="w")
        ttk.Label(frm, text="Active Template:").grid(column=0,row=1,sticky="w"); self.active_template_label = tk.StringVar(value=self.template_var.get()); ttk.Label(frm, textvariable=self.active_template_label).grid(column=1,row=1,sticky="w")
        ttk.Label(frm, text="Product:").grid(column=0,row=2,sticky="w"); self.product_combo = ttk.Combobox(frm, values=self.load_product_list()); self.product_combo.grid(column=1,row=2,sticky="w"); ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2,row=2,sticky="w")
        ttk.Label(frm, text="Weight (gross lb):").grid(column=0,row=3,sticky="w"); ttk.Entry(frm, textvariable=self.weight_var).grid(column=1,row=3,sticky="w"); ttk.Button(frm, text="Read Weight", command=self.manual_read).grid(column=2,row=3,sticky="w")
        ttk.Label(frm, text="Sell-by (YYYY-MM-DD):").grid(column=0,row=4,sticky="w"); ttk.Entry(frm, textvariable=self.sellby_var).grid(column=1,row=4,sticky="w")
        ttk.Label(frm, text="Lot:").grid(column=0,row=5,sticky="w"); ttk.Entry(frm, textvariable=self.lot_var).grid(column=1,row=5,sticky="w")
        self.preview_btn = ttk.Button(frm, text="Preview (PDF)", command=self.preview); self.preview_btn.grid(column=0,row=6,sticky="w")
        ttk.Button(frm, text="Print to Printer", command=self.print_action).grid(column=1,row=6,sticky="w")
        ttk.Button(frm, text="Start Listening", command=self.toggle_listen).grid(column=2,row=6,sticky="w")
        self.status = tk.StringVar(value="Idle"); ttk.Label(frm, textvariable=self.status).grid(column=0,row=7,columnspan=3,sticky="w")
        # refresh template selection -> enable/disable preview
        self.template_cb.bind("<<ComboboxSelected>>", lambda e: self.on_template_change())
        self.on_template_change()
    def on_template_change(self):
        tpl = self.template_var.get() or ""
        self.active_template_label.set(tpl)
        # disable preview when PRN selected
        if tpl.lower().endswith(".prn"):
            self.preview_btn.state(["disabled"])
        else:
            self.preview_btn.state(["!disabled"])
        self.settings["last_template"] = tpl; save_settings(self.settings)
    def open_options(self):
        OptionsWindow(self.root, self.settings, apply_cb=self.apply_settings)
    def apply_settings(self):
        # reload templates and settings
        self.templates = list_template_files()
        self.template_cb["values"] = self.templates
        self.template_var.set(self.settings.get("last_template") or (self.templates[0] if self.templates else ""))
        self.on_template_change()
        # update scale config
        self.scale.port = self.settings.get("scale_port","Simulate"); self.scale.baud = int(self.settings.get("scale_baud",9600))
    def load_product_list(self):
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products"); rows = c.fetchall(); conn.close()
        return [f"{r[0]} - {r[1]} (${r[2]:.2f}/lb, tare {r[3]:.3f}, PLU {r[4] or ''})" for r in rows]
    def open_product_manager(self): ProductManager(self.root, refresh_cb=self.reload_products)
    def reload_products(self): self.product_combo["values"] = self.load_product_list()
    def parse_selected_product(self):
        val = self.product_combo.get()
        if not val: messagebox.showerror("Error","Select product"); return None
        pcode = val.split(" - ")[0].strip()
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products WHERE product_code=?", (pcode,)); row = c.fetchone(); conn.close()
        if not row: messagebox.showerror("Error","Product not found"); return None
        return {"product_code":row[0],"name":row[1],"price_per_lb":row[2],"tare":row[3] or 0.0,"plu_upc":row[4]}
    def manual_read(self):
        try:
            self.scale.port = self.settings.get("scale_port", "Simulate")
            self.scale.baud = int(self.settings.get("scale_baud",9600))
            w = self.scale.read_once()
        except Exception as e:
            messagebox.showerror("Error", f"Read failed: {e}"); return
        self.weight_var.set(w); messagebox.showinfo("Weight Read", f"Gross: {w:.3f} lb")
    def generate_content(self, weight):
        prod = self.parse_selected_product(); 
        if not prod: return None
        net = max(0.0, weight - float(prod.get("tare",0.0)))
        total = round(net * prod["price_per_lb"] + 1e-9, 2)
        cents = int(round(total*100))
        upc_src = prod.get("plu_upc") or prod["product_code"]
        upc = make_price_embedded_upc(upc_src, cents)
        return {"product_name":prod["name"], "weight":net, "price_per_lb":prod["price_per_lb"], "total_price":total, "sell_by":self.sellby_var.get(), "lot":self.lot_var.get(), "upc":upc}
    def preview(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error","Invalid weight"); return
        content = self.generate_content(w); 
        if not content: return
        tpl_name = self.template_var.get()
        if not tpl_name or tpl_name.lower().endswith(".prn"): messagebox.showerror("Error","Preview not available for PRN templates"); return
        tpl_path = os.path.join(TEMPLATES_DIR, tpl_name)
        try:
            with open(tpl_path, "r", encoding="utf-8") as f: tpl = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"Failed loading template: {e}"); return
        out = os.path.abspath("preview_label.pdf")
        try:
            generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("Error", f"PDF failed: {e}"); return
        messagebox.showinfo("Preview", f"PDF generated: {out}")
        if sys.platform.startswith("win"): os.startfile(out)
    def print_action(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error","Invalid weight"); return
        content = self.generate_content(w); 
        if not content: return
        tpl_name = self.template_var.get()
        if not tpl_name: messagebox.showerror("Error","No template selected"); return
        tpl_path = os.path.join(TEMPLATES_DIR, tpl_name)
        # if PRN, load and render then send raw
        if tpl_name.lower().endswith(".prn"):
            prn = load_prn(tpl_path)
            if not prn: messagebox.showerror("Error","Failed to load PRN"); return
            rendered = render_prn(prn, content)
            try:
                send_prn_to_printer(self.settings.get("printer_port","COM1"), self.settings.get("printer_baud",38400), rendered)
                messagebox.showinfo("Printed", f"PRN sent to {self.settings.get('printer_port','COM1')}")
            except Exception as e:
                messagebox.showerror("Printer Error", str(e))
            return
        # else JSON -> generate PDF and also send DPL-equivalent to printer
        try:
            with open(tpl_path, "r", encoding="utf-8") as f: tpl = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"Failed loading template: {e}"); return
        out = os.path.abspath(f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        try:
            generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("PDF Error", str(e))
        # generate datamax commands from template and send
        datamax_cmds = generate_datamax_from_template(tpl, content, dpi=203)
        try:
            send_prn_to_printer(self.settings.get("printer_port","COM1"), self.settings.get("printer_baud",38400), datamax_cmds)
            messagebox.showinfo("Printed", f"PDF saved and Datamax commands sent to {self.settings.get('printer_port','COM1')}")
        except Exception as e:
            messagebox.showerror("Printer Error", str(e))
    def handle_scale_print(self, weight):
        # called from scale thread; marshal to GUI thread
        def job():
            self.weight_var.set(weight)
            self.status.set("Auto printing from scale")
            self.print_action()
            self.status.set("Idle")
        try: self.root.after(0, job)
        except Exception: pass
    def toggle_listen(self):
        if getattr(self.scale, "_running", False):
            self.scale.stop(); self.status.set("Idle")
        else:
            # apply settings first
            self.settings["scale_port"] = self.settings.get("scale_port", self.settings.get("scale_port","Simulate"))
            self.settings["scale_baud"] = int(self.settings.get("scale_baud", self.settings.get("scale_baud",9600)))
            self.scale.port = self.settings.get("scale_port","Simulate"); self.scale.baud = int(self.settings.get("scale_baud",9600))
            self.scale.start(); self.status.set(f"Listening on {self.scale.port}@{self.scale.baud}")
    def open_product_manager(self): ProductManager(self.root, refresh_cb=self.reload_products)
    def open_options(self): OptionsWindow(self.root, self.settings, apply_cb=self.apply_settings)

# ---------------- main ----------------
def main():
    ensure_templates(); init_db()
    root = tk.Tk(); app = App(root); root.mainloop()

if __name__ == "__main__":
    main()
