import os, sys, json, sqlite3, threading, time, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from datetime import datetime
from reportlab.lib.units import mm
try:
    import serial
except ImportError:
    serial = None

APP_NAME = "L&D Turkey Labeler"
DB_FILE = "products.db"
SETTINGS_FILE = "settings.json"
TEMPLATES_DIR = "templates"

def resource_path(rel_path):
    """Get absolute path to resource (works for dev and PyInstaller exe)"""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.abspath(".")
    return os.path.join(base, rel_path)


# optional serial/list_ports
try:
    import serial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None


def ensure_templates():
    if not os.path.exists(TEMPLATES_DIR):
        os.makedirs(TEMPLATES_DIR)
        # create a default JSON template
        default_template = {
            "name": "Default 2x2",
            "width": 50,
            "height": 50,
            "elements": [
                {"type": "text", "x": 5, "y": 45, "size": 10, "value": "{product_name}"},
                {"type": "text", "x": 5, "y": 35, "size": 8, "value": "Price/lb {price_per_lb}"},
                {"type": "text", "x": 5, "y": 25, "size": 8, "value": "Weight {weight}"},
                {"type": "text", "x": 5, "y": 15, "size": 8, "value": "Total {total_price}"}
            ]
        }
        with open(os.path.join(TEMPLATES_DIR, "default_2x2.json"), "w") as f:
            json.dump(default_template, f, indent=2)


def load_templates():
    templates = {}
    if not os.path.exists(TEMPLATES_DIR):
        return templates

    for fname in os.listdir(TEMPLATES_DIR):
        fpath = os.path.join(TEMPLATES_DIR, fname)
        if fname.lower().endswith('.json'):
            try:
                with open(fpath, 'r') as f:
                    data = json.load(f)
                templates[data.get('name', fname)] = data
            except Exception as e:
                print(f"Error loading template {fname}: {e}")
        elif fname.lower().endswith('.prn'):
            try:
                with open(fpath, 'r') as f:
                    raw = f.read()
                templates[fname] = {'type': 'prn', 'raw': raw}
            except Exception as e:
                print(f"Error loading PRN {fname}: {e}")
    return templates


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("Failed to save settings:", e)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY,
                    product_code TEXT UNIQUE,
                    name TEXT,
                    price_per_lb REAL
                 )''')
    # Add missing columns
    cols = [r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()]
    if 'tare' not in cols:
        c.execute("ALTER TABLE products ADD COLUMN tare REAL DEFAULT 0.0")
    if 'plu_upc' not in cols:
        c.execute("ALTER TABLE products ADD COLUMN plu_upc TEXT")
    conn.commit()
    conn.close()


# ---------------- scale interface ----------------

class ScaleInterface:
    def __init__(self, port="Simulate", baud=9600, simulate=False):
        self.port = port
        self.baud = baud
        self.simulate = (port == "Simulate") or simulate
        self.serial = None
        self.running = False
        self.thread = None
        self.on_print = None

    def start(self):
        if self.simulate:
            self.running = True
            self.thread = threading.Thread(target=self.sim_loop, daemon=True)
            self.thread.start()
            return

        if serial:
            try:
                self.serial = serial.Serial(self.port, self.baud, timeout=1)
                self.running = True
                self.thread = threading.Thread(target=self.read_loop, daemon=True)
                self.thread.start()
            except Exception as e:
                print("Scale connection failed:", e)

    def stop(self):
        self.running = False
        if self.serial:
            try:
                self.serial.close()
            except:
                pass

    def sim_loop(self):
        while self.running:
            time.sleep(5)
            weight = round(1.0 + 5.0 * (time.time() % 10) / 10, 2)
            if self.on_print:
                self.on_print(weight)

    def read_loop(self):
        buf = b""
        while self.running and self.serial:
            try:
                data = self.serial.read(128)
                if not data:
                    continue
                buf += data
                if b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    w = self.parse_weight(line.decode(errors="ignore"))
                    if w and self.on_print:
                        self.on_print(w)
            except Exception:
                pass

    def parse_weight(self, text):
        m = re.search(r"([0-9]+\.[0-9]+)", text)
        if m:
            return float(m.group(1))
        return None


# ---------------- printer interface ----------------

class PrinterInterface:
    """Simple serial-based printer sender for raw PRN/DPL streams."""
    def __init__(self, port="COM1", baud=38400):
        self.port = port
        self.baud = int(baud)
        self.serial = None

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial not installed")
        try:
            if self.serial and self.serial.is_open:
                return
            self.serial = serial.Serial(self.port, self.baud, timeout=2)
        except Exception as e:
            self.serial = None
            raise

    def close(self):
        try:
            if self.serial:
                try:
                    self.serial.close()
                except Exception:
                    pass
                self.serial = None
        except Exception:
            pass

    def send_bytes(self, data: bytes):
        """Send raw bytes to printer (opens/closes automatically)."""
        if serial is None:
            raise RuntimeError("pyserial not installed")
        self.open()
        try:
            self.serial.write(data)
            self.serial.flush()
        except Exception as e:
            raise
        finally:
            try:
                self.close()
            except Exception:
                pass

    def send_text(self, txt: str, encoding='ascii'):
        return self.send_bytes(txt.encode(encoding, errors='replace'))


# ---------------- label rendering / PRN substitution ----------------

# Ensure mm is available (some fragments expect mm)
try:
    from reportlab.lib.units import mm
except Exception:
    mm = 1.0

def render_pdf(template, product, weight, out_path="label.pdf"):
    """
    Render a PDF using a simplified template object.
    Template structure expected:
      { "elements": [ { "type":"text", "x":<mm>, "y":<mm>, "size":<pt>, "value":"{product_name}" }, ... ] }
    """
    try:
        # page size derived from template or fallback letter
        width_mm = template.get("width", 50)
        height_mm = template.get("height", 50)
    except Exception:
        width_mm, height_mm = 50, 50

    try:
        c = canvas.Canvas(out_path, pagesize=(width_mm * mm, height_mm * mm))
    except Exception:
        # fallback to letter-sized if canvas creation fails
        c = canvas.Canvas(out_path)

    price_per_lb = product.get("price_per_lb", 0.0)
    tare = product.get("tare", 0.0)
    net_weight = max(0.0, weight - tare)
    total_price = round(net_weight * price_per_lb + 1e-9, 2)

    subs = {
        "product_code": product.get("product_code", ""),
        "product_name": product.get("name", ""),
        "price_per_lb": f"{price_per_lb:.2f}",
        "weight": f"{net_weight:.3f}",
        "total_price": f"{total_price:.2f}",
        "sell_by": product.get("sell_by", ""),
        "lot": product.get("lot", ""),
        "upc": product.get("plu_upc", product.get("product_code", ""))
    }

    for el in template.get("elements", template.get("fields", [])):
        t = el.get("type", "text")
        x = float(el.get("x", 0)) * mm
        y = float(el.get("y", 0)) * mm
        size = int(el.get("size", 8))
        if t == "text":
            val = el.get("value", "")
            # substitute {placeholders}
            for k, v in subs.items():
                val = val.replace("{" + k + "}", str(v))
            try:
                c.setFont(template.get("font", "Helvetica"), size)
            except Exception:
                c.setFont("Helvetica", size)
            c.drawString(x, y, val)
        elif t == "image" and ImageReader is not None:
            # optional image
            img_path = el.get("value", "")
            try:
                ir = ImageReader(img_path)
                w = el.get("width_mm", 10) * mm
                h = el.get("height_mm", 10) * mm
                c.drawImage(ir, x, y, width=w, height=h)
            except Exception:
                pass
        elif t == "barcode":
            code = subs.get("upc", "")
            try:
                drawing = createBarcodeDrawing("UPCA", value=code, barHeight=el.get("height", 10) * mm, humanReadable=True)
                drawing.drawOn(c, x, y)
            except Exception:
                c.drawString(x, y, "UPC:" + str(code))

    c.showPage()
    c.save()
    return out_path


def load_prn_text(path):
    """Load raw PRN file text (used for placeholder substitution)."""
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception:
        try:
            with open(path, 'r', encoding='latin-1', errors='ignore') as f:
                return f.read()
        except Exception:
            return None


def render_prn_template(path, content):
    """
    Replace placeholders in PRN text. Supports {name} style placeholders and some common tags.
    content: dict containing keys like product_name, weight, total_price, sell_by, lot, upc
    """
    txt = load_prn_text(path)
    if txt is None:
        return None
    subs = {
        'product_name': content.get('product_name', ''),
        'weight': f"{float(content.get('weight', 0)):.3f}",
        'price_per_lb': f"{float(content.get('price_per_lb', 0)):.2f}",
        'total_price': f"{float(content.get('total_price', 0)):.2f}",
        'sell_by': content.get('sell_by', ''),
        'lot': content.get('lot', ''),
        'upc': content.get('upc', '')
    }
    # curly braces replacement
    for k, v in subs.items():
        txt = re.sub(r"\{" + re.escape(k) + r"\}", str(v), txt, flags=re.IGNORECASE)
    # some PRN templates use angle tags like <KillDate> etc.
    angle_map = {
        '<KillDate>': subs.get('sell_by', ''),
        '<WtLbs>': subs.get('weight', ''),
        '<Price>': subs.get('total_price', ''),
        '<PLU>': subs.get('upc', '')
    }
    for a, b in angle_map.items():
        txt = txt.replace(a, str(b))
    return txt


def send_prn_to_printer(port, baud, payload_text):
    """Send rendered PRN text to printer via serial."""
    if serial is None:
        raise RuntimeError("pyserial not installed")
    data = payload_text.encode('ascii', errors='replace')
    p = PrinterInterface(port=port, baud=baud)
    p.send_bytes(data)


# ---------------- simple Datamax/Datamax-like generator (fallback) ----------------

def inches_to_dots(val_in, dpi=203):
    return int(round(val_in * dpi))

def generate_datamax_from_template(template, content, dpi=203):
    """
    Create a simple Datamax-like command stream from a JSON template.
    This is a fallback when you want to send DATAMAX commands instead of PRN or PDF.
    """
    lines = ['N']
    size_in = template.get('size_in', [LABEL_WIDTH_IN, LABEL_HEIGHT_IN])
    qx = inches_to_dots(size_in[0], dpi)
    qy = inches_to_dots(size_in[1], dpi)
    lines.append(f"q{qx}")
    lines.append(f"Q{qy},24")
    for fld in template.get('fields', []):
        name = fld.get('name')
        x = inches_to_dots(fld.get('x', 0), dpi)
        y = inches_to_dots(fld.get('y', 0), dpi)
        if name == 'barcode':
            code = content.get('upc', '')
            h = inches_to_dots(fld.get('height', 0.3), dpi)
            # B,x,y,rotation,barcode,barwidth,barmultipler,height,human readable,"data"
            lines.append(f'B{x},{y},0,UPCA,2,2,{h},N,"{code}"')
        else:
            # default to text A (font 3) - adjust as needed for your printer
            text = str(content.get(name, ''))
            # protect double quotes
            text = text.replace('"', "'")
            lines.append(f'A{x},{y},0,3,1,1,N,"{text}"')
    lines.append('P1')
    return '\n'.join(lines)


# ---------------- product manager (GUI) ----------------

class ProductManager(tk.Toplevel):
    def __init__(self, parent, refresh_cb):
        super().__init__(parent)
        self.title("Product Manager")
        self.geometry("700x420")
        self.refresh_cb = refresh_cb
        self.build_ui()
        self.load()

    def build_ui(self):
        frm = ttk.Frame(self, padding=8); frm.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(frm, columns=('code','name','price','tare','plu'), show='headings')
        for col, txt in (('code','Code'), ('name','Name'), ('price','Price/lb'),
                         ('tare','Tare'), ('plu','PLU')):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=140)
        self.tree.pack(fill='both', expand=True)
        btns = ttk.Frame(frm); btns.pack(fill='x', pady=6)
        ttk.Button(btns, text='Add', command=self.add).pack(side='left', padx=4)
        ttk.Button(btns, text='Edit', command=self.edit).pack(side='left', padx=4)
        ttk.Button(btns, text='Delete', command=self.delete).pack(side='left', padx=4)

    def load(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT product_code, name, price_per_lb, tare, plu_upc FROM products')
        for r in c.fetchall():
            self.tree.insert('', 'end', values=(r[0], r[1], f"{r[2]:.2f}", f"{r[3]:.3f}", r[4] or ''))
        conn.close()

    def add(self):
        self.editor()

    def edit(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])['values']
        self.editor(vals)

    def delete(self):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])['values']
        if messagebox.askyesno('Delete', f"Delete {vals[0]}?"):
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('DELETE FROM products WHERE product_code=?', (vals[0],))
            conn.commit()
            conn.close()
            self.load()
            self.refresh_cb()

    def editor(self, vals=None):
        w = tk.Toplevel(self)
        labels = ['Product Code', 'Name', 'Price per lb', 'Tare (lb)', 'PLU/UPC']
        vars = []
        defaults = vals if vals else ('', '', 0.0, 0.0, '')
        for i, lbl in enumerate(labels):
            ttk.Label(w, text=lbl).grid(column=0, row=i, sticky='w')
            var = tk.StringVar(value=defaults[i])
            ttk.Entry(w, textvariable=var).grid(column=1, row=i, sticky='ew')
            vars.append(var)

        def save():
            code = vars[0].get().strip()
            name = vars[1].get().strip()
            try:
                price = float(vars[2].get())
            except Exception:
                price = 0.0
            try:
                tare = float(vars[3].get())
            except Exception:
                tare = 0.0
            plu = vars[4].get().strip()
            if not code or not name:
                messagebox.showerror('Error', 'Code and Name required')
                return
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO products (product_code, name, price_per_lb, tare, plu_upc) VALUES (?,?,?,?,?)',
                      (code, name, price, tare, plu))
            conn.commit()
            conn.close()
            w.destroy()
            self.load()
            self.refresh_cb()

        ttk.Button(w, text='Save', command=save).grid(column=0, row=len(labels), columnspan=2)


# ---------------- main GUI ----------------

class App:
    def __init__(self, root):
        self.root = root
        root.title("L+D Turkey Labeler")

        ensure_templates()
        init_db()
        self.settings = load_settings()

        # Use custom templates dir if set
        if self.settings.get("templates_dir"):
            global TEMPLATES_DIR
            TEMPLATES_DIR = self.settings["templates_dir"]

        self.templates = load_templates()
        if not self.settings.get("last_template") and self.templates:
            self.settings["last_template"] = list(self.templates.keys())[0]

        self.conn = sqlite3.connect(DB_FILE)
        port = self.settings.get("last_port", "Simulate")
        baud = self.settings.get("last_baud", 9600)

        self.scale = ScaleInterface(port=port, baud=baud)
        self.scale.on_print = self.handle_scale_print

        # build UI
        self.build()

    def build(self):
        frm = ttk.Frame(self.root, padding=10)
        frm.grid()

        # Port selection
        ttk.Label(frm, text="Scale Port:").grid(column=0, row=0, sticky="w")
        self.port_var = tk.StringVar(value=self.settings.get("last_port", "Simulate"))
        self.port_cb = ttk.Combobox(
            frm, textvariable=self.port_var, values=["Simulate"] + self.enumerate_ports(), state="readonly"
        )
        self.port_cb.grid(column=1, row=0, sticky="w")

        ttk.Label(frm, text="Baud:").grid(column=2, row=0, sticky="w")
        self.baud_var = tk.StringVar(value=str(self.settings.get("last_baud", 9600)))
        self.baud_cb = ttk.Combobox(
            frm, textvariable=self.baud_var, values=["9600", "19200", "38400", "57600"], state="readonly"
        )
        self.baud_cb.grid(column=3, row=0, sticky="w")

        # Template selection
        ttk.Label(frm, text="Template:").grid(column=0, row=1, sticky="w")
        self.template_var = tk.StringVar(value=self.settings.get("last_template"))
        self.template_cb = ttk.Combobox(
            frm, textvariable=self.template_var, values=list(self.templates.keys()), state="readonly"
        )
        self.template_cb.grid(column=1, row=1, sticky="w")

        # Product selection
        ttk.Label(frm, text="Product:").grid(column=0, row=2, sticky="w")
        self.product_combo = ttk.Combobox(frm, values=self.load_product_list())
        self.product_combo.grid(column=1, row=2, sticky="w")
        ttk.Button(frm, text="Manage Products", command=self.open_product_manager).grid(column=2, row=2)

        # Weight display
        ttk.Label(frm, text="Weight (gross lb):").grid(column=0, row=3, sticky="w")
        self.weight_var = tk.DoubleVar(value=0.0)
        ttk.Entry(frm, textvariable=self.weight_var).grid(column=1, row=3, sticky="w")
        ttk.Button(frm, text="Read Weight", command=self.manual_read).grid(column=2, row=3)

        # Sell-by and lot
        ttk.Label(frm, text="Sell-by:").grid(column=0, row=4, sticky="w")
        self.sellby = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(frm, textvariable=self.sellby).grid(column=1, row=4, sticky="w")

        ttk.Label(frm, text="Lot:").grid(column=0, row=5, sticky="w")
        self.lot = tk.StringVar(value="")
        ttk.Entry(frm, textvariable=self.lot).grid(column=1, row=5, sticky="w")

        # Buttons
        ttk.Button(frm, text="Preview & Generate", command=self.preview).grid(column=0, row=6)
        ttk.Button(frm, text="Print (to file)", command=self.print_file).grid(column=1, row=6)
        ttk.Button(frm, text="Options", command=self.open_options).grid(column=2, row=6)

        # Status
        self.status = tk.StringVar(value="Idle")
        ttk.Label(frm, textvariable=self.status).grid(column=0, row=7, columnspan=3, sticky="w")

        # Close
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def load_product_list(self):
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT product_code, name, price_per_lb, tare, plu_upc FROM products")
        rows = c.fetchall()
        conn.close()
        return [
            f"{r[0]} - {r[1]} (${r[2]:.2f}/lb, tare {r[3]:.3f}, PLU {r[4] or ''})"
            for r in rows
        ]

    def reload_products(self):
        self.product_combo["values"] = self.load_product_list()

    def manual_read(self):
        try:
            w = self.scale.read_once()
        except Exception as e:
            messagebox.showerror("Error", f"Read failed: {e}")
            return
        self.weight_var.set(w)
        messagebox.showinfo("Weight Read", f"Gross: {w:.3f} lb")

    def open_product_manager(self):
        ProductManager(self.root, refresh_cb=self.reload_products)

    def parse_product(self):
        v = self.product_combo.get()
        if not v:
            messagebox.showerror("Error", "Select product")
            return None
        code = v.split(" - ")[0].strip()
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT product_code, name, price_per_lb, tare, plu_upc FROM products WHERE product_code=?", (code,))
        row = c.fetchone()
        conn.close()
        if not row:
            messagebox.showerror("Error", "Product not found")
            return None
        return {
            "product_code": row[0],
            "name": row[1],
            "price_per_lb": row[2],
            "tare": row[3] or 0.0,
            "plu_upc": row[4]
        }

    def generate_content(self, weight):
        prod = self.parse_product()
        if not prod:
            return None
        net = max(0.0, weight - float(prod["tare"] or 0.0))
        total = round(net * prod["price_per_lb"] + 1e-9, 2)
        cents = int(round(total * 100))
        upc_src = prod["plu_upc"] or prod["product_code"]
        upc_code = make_price_embedded_upc(upc_src, cents)
        return {
            "product_name": prod["name"],
            "weight": net,
            "price_per_lb": prod["price_per_lb"],
            "total_price": total,
            "sell_by": self.sellby.get(),
            "lot": self.lot.get(),
            "upc": upc_code
        }

    def preview(self):
        try:
            w = float(self.weight_var.get())
        except Exception:
            messagebox.showerror("Error", "Invalid weight")
            return
        content = self.generate_content(w)
        if not content:
            return
        tpl = load_templates().get(self.template_var.get())
        if not tpl:
            messagebox.showerror("Error", "Select template")
            return
        out = os.path.abspath("preview_label.pdf")
        try:
            generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("Error", f"PDF failed: {e}")
            return
        messagebox.showinfo("Preview", f"PDF: {out}")
        if sys.platform.startswith("win"):
            os.startfile(out)

    def print_file(self):
        try:
            w = float(self.weight_var.get())
        except Exception:
            messagebox.showerror("Error", "Invalid weight")
            return
        content = self.generate_content(w)
        if not content:
            return
        tpl = load_templates().get(self.template_var.get())
        out = os.path.abspath(f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        try:
            generate_label_pdf(out, tpl, content)
        except Exception as e:
            messagebox.showerror("Error", f"PDF failed: {e}")
            return
        messagebox.showinfo("Saved", f"Saved: {out}")

    def handle_scale_print(self, weight):
        def do_auto():
            self.weight_var.set(weight)
            content = self.generate_content(weight)
            if not content:
                self.status.set("Auto skipped (no product)")
                return
            tpl = load_templates().get(self.template_var.get())
            if not tpl:
                self.status.set("Auto skipped (no template)")
                return
            out = os.path.abspath(f"label_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            try:
                generate_label_pdf(out, tpl, content)
                self.status.set(f"Auto-saved {os.path.basename(out)}")
            except Exception as e:
                self.status.set(f"Auto failed: {e}")
        try:
            self.root.after(0, do_auto)
        except Exception:
            pass

    def open_options(self):
        OptionsWindow(self.root, self.settings, self.apply_options)

    def apply_options(self, new_settings):
        self.settings.update(new_settings)
        save_settings(self.settings)
        self.port_var.set(self.settings.get("last_port", "Simulate"))
        self.baud_var.set(str(self.settings.get("last_baud", 9600)))
        self.template_var.set(self.settings.get("last_template"))

    def on_close(self):
        try:
            self.scale.stop()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        # persist settings
        self.settings["last_port"] = self.port_var.get()
        self.settings["last_baud"] = int(self.baud_var.get())
        self.settings["last_template"] = self.template_var.get()
        save_settings(self.settings)
        self.root.destroy()


# ---------------- options window (sub-GUI) ----------------

class OptionsWindow(tk.Toplevel):
    def __init__(self, parent, settings, save_callback):
        super().__init__(parent)
        self.title("Options")
        self.geometry("400x300")
        self.settings = settings.copy()
        self.save_callback = save_callback
        self.build_ui()

    def build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        # Scale port
        ttk.Label(frm, text="Scale Port:").grid(row=0, column=0, sticky="w")
        self.scale_port = tk.StringVar(value=self.settings.get("last_port", "Simulate"))
        ttk.Entry(frm, textvariable=self.scale_port).grid(row=0, column=1, sticky="ew")

        # Scale baud
        ttk.Label(frm, text="Scale Baud:").grid(row=1, column=0, sticky="w")
        self.scale_baud = tk.StringVar(value=str(self.settings.get("last_baud", 9600)))
        ttk.Entry(frm, textvariable=self.scale_baud).grid(row=1, column=1, sticky="ew")

        # Printer port
        ttk.Label(frm, text="Printer Port:").grid(row=2, column=0, sticky="w")
        self.printer_port = tk.StringVar(value=self.settings.get("printer_port", "COM1"))
        ttk.Entry(frm, textvariable=self.printer_port).grid(row=2, column=1, sticky="ew")

        # Printer baud
        ttk.Label(frm, text="Printer Baud:").grid(row=3, column=0, sticky="w")
        self.printer_baud = tk.StringVar(value=str(self.settings.get("printer_baud", 38400)))
        ttk.Entry(frm, textvariable=self.printer_baud).grid(row=3, column=1, sticky="ew")

        # Templates dir
        ttk.Label(frm, text="Templates Dir:").grid(row=4, column=0, sticky="w")
        self.templates_dir = tk.StringVar(value=self.settings.get("templates_dir", TEMPLATES_DIR))
        ttk.Entry(frm, textvariable=self.templates_dir).grid(row=4, column=1, sticky="ew")

        # Save & Close button
        ttk.Button(frm, text="Save", command=self.save).grid(row=5, column=0, columnspan=2, pady=10)

        frm.columnconfigure(1, weight=1)

    def save(self):
        new_settings = {
            "last_port": self.scale_port.get(),
            "last_baud": int(self.scale_baud.get()),
            "printer_port": self.printer_port.get(),
            "printer_baud": int(self.printer_baud.get()),
            "templates_dir": self.templates_dir.get()
        }
        self.save_callback(new_settings)
        self.destroy()


# ---------------- main ----------------

def main():
    ensure_templates()
    init_db()
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

