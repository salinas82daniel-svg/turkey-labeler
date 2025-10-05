"""L+D Turkey Labeler — Full Version with Logo Support, external templates, baud selection, and fixes"""

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

# reportlab for PDF & barcode & images
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
    from tkinter import ttk, messagebox
except Exception:
    print('Tkinter is required but not available. On Debian/Ubuntu install python3-tk.')
    sys.exit(1)

# ---------------- config ----------------
DB_FILE = 'products.db'
TEMPLATES_DIR = 'templates'
SETTINGS_FILE = 'settings.json'
LABEL_WIDTH_IN = 2.0
LABEL_HEIGHT_IN = 2.0

# ---------------- helpers ----------------
def resource_path(rel_path):
    """Return absolute path for a resource (works both in dev and PyInstaller onefile).
    Pass a path relative to your project root (e.g. 'templates/logo.png').
    if getattr(sys, "frozen", False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel_path)

def ensure_templates():
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    example_path = os.path.join(TEMPLATES_DIR, 'default_2x2.json')
    if not os.path.exists(example_path):
        sample = {
            "name": "default_2x2",
            "size_in": [2.0, 2.0],
            "font": "Helvetica",
            "fields": [
                {"name": "logo", "x": 0.1, "y": 1.7, "width": 0.8, "height": 0.5, "path": "logo.png"},
                {"name": "product_name", "x": 0.1, "y": 1.5, "size": 10},
                {"name": "weight", "x": 0.1, "y": 1.3, "size": 9},
                {"name": "price_per_lb", "x": 0.1, "y": 1.1, "size": 9},
                {"name": "total_price", "x": 0.1, "y": 0.9, "size": 12},
                {"name": "sell_by", "x": 0.1, "y": 0.7, "size": 8},
                {"name": "lot", "x": 0.1, "y": 0.5, "size": 8},
                {"name": "barcode", "x": 0.1, "y": 0.05, "width": 1.8, "height": 0.5}
            ]
        }
        with open(example_path, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2)

def load_templates():
    templates = {}
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
        bundle_dir = os.path.join(getattr(sys, "_MEIPASS", ""), "templates")
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        bundle_dir = os.path.join(base_dir, "templates")
    search_paths = []
    external_dir = os.path.join(base_dir, "templates")
    if os.path.isdir(external_dir):
        search_paths.append(external_dir)
    if os.path.isdir(bundle_dir) and bundle_dir not in search_paths:
        search_paths.append(bundle_dir)
    for folder in search_paths:
        try:
            for fn in os.listdir(folder):
                if fn.lower().endswith(".json"):
                    try:
                        with open(os.path.join(folder, fn), "r", encoding="utf-8") as f:
                            d = json.load(f)
                            templates[d.get("name", fn)] = d
                    except Exception as e:
                        print("Template load error:", fn, e)
        except Exception:
            pass
    return templates

# ---------------- db + migration ----------------
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
    conn.commit()
    conn.close()

# ---------------- settings ----------------
def load_settings():
    defaults = {"last_port": "Simulate", "last_template": None, "auto_listen": False, "last_baud": 9600}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    return defaults

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass

# ---------------- upc helpers ----------------
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

# ---------------- scale interface ----------------
class ScaleInterface:
    def __init__(self, port="Simulate", baud=9600):
        self.port = port
        self.baud = baud
        self.simulate = (port == "Simulate")
        self._thread = None
        self._running = False
        self.on_print = None
        self._last_trigger = 0.0

    def start(self):
        if self._running:
            return
        self._running = True
        self.simulate = (self.port == "Simulate")
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
            w = round(random.uniform(0.5, 8.0), 2)
            self._trigger(w)

    def _read_loop(self):
        try:
            if serial is None:
                raise RuntimeError("pyserial not available")
            with serial.Serial(self.port, self.baud, timeout=1) as ser:
                while self._running:
                    try:
                        raw = ser.readline().decode(errors="ignore").strip()
                    except Exception:
                        continue
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
            print("Scale read thread error:", e)

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

# ---------------- pdf generation ----------------
def generate_label_pdf(path, template, content):
    if createBarcodeDrawing is None:
        raise RuntimeError("reportlab not installed")
    width_in, height_in = template.get("size_in", [LABEL_WIDTH_IN, LABEL_HEIGHT_IN])
    w = width_in * inch; h = height_in * inch
    c = canvas.Canvas(path, pagesize=(w, h))
    for fld in template.get("fields", []):
        name = fld.get("name")
        x = fld.get("x", 0) * inch; y = fld.get("y", 0) * inch; size = fld.get("size", 8)
        if name == "barcode":
            code = content.get("upc", "")
            try:
                drawing = createBarcodeDrawing("UPCA", value=code, barHeight=fld.get("height", 0.3) * inch, humanReadable=True)
                drawing.drawOn(c, x, y)
            except Exception:
                c.drawString(x, y, "UPC:" + code)
            continue
        if name == "logo":
            if ImageReader is None:
                c.drawString(x, y, "[No image support: reportlab missing]")
                continue
            img_path = fld.get("path") or fld.get("file") or "logo.png"
            if not os.path.isabs(img_path):
                candidate = os.path.join(TEMPLATES_DIR, img_path)
            else:
                candidate = img_path
            candidate = resource_path(candidate)
            if not os.path.exists(candidate):
                candidate = resource_path(img_path)
            if os.path.exists(candidate):
                try:
                    iw = fld.get("width", 0.5) * inch; ih = fld.get("height", 0.5) * inch
                    c.drawImage(ImageReader(candidate), x, y, width=iw, height=ih, mask="auto")
                except Exception:
                    c.drawString(x, y, "[Image error]")
            else:
                c.drawString(x, y, "[Image not found]")
            continue
        txt = ""
        if name == "product_name":
            txt = content.get("product_name", "")
        elif name == "weight":
            txt = f"Weight: {content.get('weight', 0):.3f} lb"
        elif name == "price_per_lb":
            txt = f"{content.get('price_per_lb', 0):.2f} /lb"
        elif name == "total_price":
            txt = f"Total: ${content.get('total_price', 0):.2f}"
        elif name == "sell_by":
            txt = f"Sell by: {content.get('sell_by', '')}"
        elif name == "lot":
            txt = f"Lot: {content.get('lot', '')}"
        else:
            txt = str(content.get(name, ""))
        c.setFont(template.get("font", "Helvetica"), size)
        c.drawString(x, y, txt)
    c.showPage(); c.save()

# ---------------- product manager GUI ----------------
class ProductManager(tk.Toplevel):
    def __init__(self, parent, refresh_cb):
        super().__init__(parent)
        self.title("Product Manager")
        self.geometry("640x360")
        self.refresh_cb = refresh_cb
        self.build_ui()
        self.load()
    def build_ui(self):
        frm = ttk.Frame(self, padding=8); frm.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frm, columns=("code","name","price","tare","plu"), show="headings")
        for col,txt in (("code","Code"),("name","Name"),("price","Price/lb"),("tare","Tare"),("plu","PLU")):
            self.tree.heading(col, text=txt); self.tree.column(col, width=120)
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(frm); btns.pack(fill="x")
        ttk.Button(btns, text="Add", command=self.add).pack(side="left")
        ttk.Button(btns, text="Edit", command=self.edit).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.delete).pack(side="left")
    def load(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products")
        for r in c.fetchall(): self.tree.insert("", "end", values=(r[0], r[1], f"{r[2]:.2f}", f"{r[3]:.3f}", r[4] or ""))
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
        w = tk.Toplevel(self); w.title("Edit Product")
        labels = ["Product Code","Name","Price per lb","Tare (lb)","PLU/UPC"]
        vars = []
        defaults = vals if vals else ("","", "0.00", "0.000", "")
        for i, lbl in enumerate(labels):
            ttk.Label(w, text=lbl).grid(column=0, row=i, sticky="w")
            var = tk.StringVar(value=defaults[i])
            ttk.Entry(w, textvariable=var).grid(column=1, row=i, sticky="ew")
            vars.append(var)
        def save():
            code = vars[0].get().strip(); name = vars[1].get().strip()
            try: price = float(vars[2].get())
            except Exception: price = 0.0
            try: tare = float(vars[3].get())
            except Exception: tare = 0.0
            plu = vars[4].get().strip()
            if not code or not name: messagebox.showerror("Error","Code and Name required"); return
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO products (product_code,name,price_per_lb,tare,plu_upc) VALUES (?,?,?,?,?)", (code, name, price, tare, plu))
            conn.commit(); conn.close(); w.destroy(); self.load(); self.refresh_cb()
        ttk.Button(w, text="Save", command=save).grid(column=0, row=len(labels), columnspan=2, sticky="ew")

# ---------------- main GUI ----------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("L+D Turkey Labeler")
        ensure_templates(); init_db()
        self.settings = load_settings(); self.templates = load_templates()
        if not self.settings.get("last_template") and self.templates:
            self.settings["last_template"] = list(self.templates.keys())[0]
        self.conn = sqlite3.connect(DB_FILE)
        port = self.settings.get("last_port", "Simulate")
        baud = self.settings.get("last_baud", 9600)
        self.scale = ScaleInterface(port=port, baud=baud)
        self.scale.on_print = self.handle_scale_print
        self.build()

    def build(self):
        frm = ttk.Frame(self.root, padding=10); frm.grid()
        ttk.Label(frm, text="Port:").grid(column=0, row=0, sticky="w")
        self.port_var = tk.StringVar(value=self.settings.get("last_port", "Simulate"))
        self.port_cb = ttk.Combobox(frm, textvariable=self.port_var, values=["Simulate"] + self.enumerate_ports(), state="readonly")
        self.port_cb.grid(column=1, row=0, sticky="w")
        ttk.Button(frm, text="Refresh", command=self.refresh_ports).grid(column=2, row=0)
        self.listen_btn = ttk.Button(frm, text="Start Listening", command=self.toggle_listen); self.listen_btn.grid(column=3, row=0)
        ttk.Label(frm, text="Baud:").grid(column=0, row=1, sticky="w")
        self.baud_var = tk.IntVar(value=self.settings.get("last_baud", 9600))
        self.baud_cb = ttk.Combobox(frm, textvariable=self.baud_var, values=[9600, 19200, 38400, 57600], state="readonly")
        self.baud_cb.grid(column=1, row=1, sticky="w")
        ttk.Label(frm, text="Template:").grid(column=0, row=2, sticky="w")
        self.template_var = tk.StringVar(value=self.settings.get("last_template"))
        self.template_cb = ttk.Combobox(frm, textvariable=self.template_var, values=list(self.templates.keys()), state="readonly")
        self.template_cb.grid(column=1, row=2, sticky="w")
        self.template_cb.bind("<Button-1>", self.refresh_template_list)
        ttk.Label(frm, text="Product:").grid(column=0, row=3, sticky="w")
        self.product_combo = ttk.Combobox(frm, values=self.load_product_list()); self.product_combo.grid(column=1, row=3, sticky="w")
        ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2, row=3)
        ttk.Label(frm, text="Weight (gross lb):").grid(column=0, row=4, sticky="w")
        self.weight_var = tk.DoubleVar(value=0.0); ttk.Entry(frm, textvariable=self.weight_var).grid(column=1, row=4, sticky="w")
        ttk.Button(frm, text="Read Weight", command=self.manual_read).grid(column=2, row=4)
        ttk.Label(frm, text="Sell-by:").grid(column=0, row=5, sticky="w"); self.sellby = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d")); ttk.Entry(frm, textvariable=self.sellby).grid(column=1, row=5, sticky="w")
        ttk.Label(frm, text="Lot:").grid(column=0, row=6, sticky="w"); self.lot = tk.StringVar(value=""); ttk.Entry(frm, textvariable=self.lot).grid(column=1, row=6, sticky="w")
        ttk.Button(frm, text="Preview & Generate", command=self.preview).grid(column=0, row=7); ttk.Button(frm, text="Print (to file)", command=self.print_file).grid(column=1, row=7)
        self.status = tk.StringVar(value="Idle"); ttk.Label(frm, textvariable=self.status).grid(column=0, row=8, columnspan=3, sticky="w")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def enumerate_ports(self):
        ports = []
        if list_ports is not None:
            try:
                for p in list_ports.comports(): ports.append(p.device)
            except Exception:
                pass
        for i in range(1, 21):
            n = f"COM{i}"
            if n not in ports: ports.append(n)
        return ports

    def refresh_ports(self): self.port_cb["values"] = ["Simulate"] + self.enumerate_ports()

    def toggle_listen(self):
        if getattr(self.scale, "_running", False): self.stop_listen()
        else: self.start_listen()

    def start_listen(self):
        port = self.port_var.get(); baud = int(self.baud_var.get())
        self.settings["last_port"] = port; self.settings["last_baud"] = baud; save_settings(self.settings)
        try: self.scale.stop()
        except Exception: pass
        self.scale = ScaleInterface(port=port, baud=baud)
        self.scale.on_print = self.handle_scale_print
        try:
            self.scale.start(); self.listen_btn.config(text="Stop Listening"); self.status.set(f"Listening on {port}@{baud}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start listener: {e}")

    def stop_listen(self):
        try: self.scale.stop(); self.listen_btn.config(text="Start Listening"); self.status.set("Idle")
        except Exception: pass

    def load_product_list(self):
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products"); rows = c.fetchall(); conn.close()
        return [f"{r[0]} - {r[1]} (${r[2]:.2f}/lb, tare {r[3]:.3f}, PLU {r[4] or ""})" for r in rows]

    def reload_products(self): self.product_combo["values"] = self.load_product_list()

    def manual_read(self):
        try: w = self.scale.read_once()
        except Exception as e: messagebox.showerror("Error", f"Read failed: {e}"); return
        self.weight_var.set(w); messagebox.showinfo("Weight Read", f"Gross: {w:.3f} lb")

    def open_product_manager(self): ProductManager(self.root, refresh_cb=self.reload_products)

    def parse_product(self):
        v = self.product_combo.get()
        if not v: messagebox.showerror("Error", "Select product"); return None
        code = v.split(" - ")[0].strip()
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("SELECT product_code,name,price_per_lb,tare,plu_upc FROM products WHERE product_code=?", (code,)); row = c.fetchone(); conn.close()
        if not row: messagebox.showerror("Error", "Product not found"); return None
        return {"product_code": row[0], "name": row[1], "price_per_lb": row[2], "tare": row[3] or 0.0, "plu_upc": row[4]}

    def generate_content(self, weight):
        prod = self.parse_product()
        if not prod: return None
        net = max(0.0, weight - float(prod["tare"] or 0.0))
        total = round(net * prod["price_per_lb"] + 1e-9, 2)
        cents = int(round(total * 100))
        upc_src = prod["plu_upc"] or prod["product_code"]
        upc_code = make_price_embedded_upc(upc_src, cents)
        return {"product_name": prod["name"], "weight": net, "price_per_lb": prod["price_per_lb"], "total_price": total, "sell_by": self.sellby.get(), "lot": self.lot.get(), "upc": upc_code}

    def preview(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error", "Invalid weight"); return
        content = self.generate_content(w)
        if not content: return
        tpl = load_templates().get(self.template_var.get())
        if not tpl: messagebox.showerror("Error", "Select template"); return
        out = os.path.abspath("preview_label.pdf")
        try: generate_label_pdf(out, tpl, content)
        except Exception as e: messagebox.showerror("Error", f"PDF failed: {e}"); return
        messagebox.showinfo("Preview", f"PDF: {out}")
        if sys.platform.startswith("win"): os.startfile(out)

    def print_file(self):
        try: w = float(self.weight_var.get())
        except Exception: messagebox.showerror("Error", "Invalid weight"); return
        content = self.generate_content(w)
        if not content: return
        tpl = load_templates().get(self.template_var.get())
        out = os.path.abspath(f"label_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf")
        try: generate_label_pdf(out, tpl, content)
        except Exception as e: messagebox.showerror("Error", f"PDF failed: {e}"); return
        messagebox.showinfo("Saved", f"Saved: {out}")

    def handle_scale_print(self, weight):
        def do_auto():
            self.weight_var.set(weight)
            content = self.generate_content(weight)
            if not content: self.status.set("Auto skipped (no product)"); return
            tpl = load_templates().get(self.template_var.get())
            if not tpl: self.status.set("Auto skipped (no template)"); return
            out = os.path.abspath(f"label_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf")
            try: generate_label_pdf(out, tpl, content); self.status.set(f"Auto-saved {os.path.basename(out)}")
            except Exception as e: self.status.set(f"Auto failed: {e}")
        try: self.root.after(0, do_auto)
        except Exception: pass

    def refresh_template_list(self, event=None):
        try:
            self.templates = load_templates()
            vals = list(self.templates.keys())
            self.template_cb["values"] = vals
            if vals and (self.template_var.get() not in vals):
                self.template_var.set(vals[0])
        except Exception as e:
            print("Refresh templates failed:", e)

    def on_close(self):
        try: self.scale.stop()
        except Exception: pass
        try: self.conn.close()
        except Exception: pass
        self.settings["last_port"] = self.port_var.get()
        self.settings["last_template"] = self.template_var.get()
        try:
            self.settings["last_baud"] = int(self.baud_var.get())
        except Exception:
            self.settings["last_baud"] = 9600
        save_settings(self.settings)
        self.root.destroy()

# ---------------- main ----------------
def main():
    ensure_templates(); init_db()
    root = tk.Tk(); app = App(root); root.mainloop()

if __name__ == "__main__":
    main()