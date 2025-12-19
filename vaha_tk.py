import os
import tkinter as tk
from tkinter import messagebox
import serial
import serial.tools.list_ports
import threading
import time
import re
import gc

BAUD = 9600

re_no  = re.compile(r"^No\.\s*:\s*(\d+)\s*$")
re_nw  = re.compile(r"^N\.W\.\s*:\s*([-\d.,]+)\s*([a-zA-Z]*)\s*$")
re_uw  = re.compile(r"^U\.W\.\s*:\s*([-\d.,]+)\s*([a-zA-Z]*)\s*$")
re_pcs = re.compile(r"^PCS\.\s*:\s*(\d+)\s*$")


def resolve_compile_time():
    cached = globals().get("__cached__") or ""
    path = cached if cached and os.path.exists(cached) else __file__

    try:
        ts = os.path.getmtime(path)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return None

def find_ch340():
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "")
        if "CH340" in desc:
            return p.device, desc
    return None, None

class App:
    def __init__(self, root):
        self.root = root
        compile_time = resolve_compile_time()
        title = "Váha – rychlý přehled"
        if compile_time:
            title = f"{title} – kompilace: {compile_time}"

        root.title(title)
        root.geometry("520x220")

        self.ser = None
        self.running = False
        self.thread = None

        self.buf = ""
        self.lock = threading.Lock()

        self.last_no = "-"
        self.last_nw = "-"
        self.last_uw = "-"
        self.last_pcs = "-"

        top = tk.Frame(root)
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.btn_connect = tk.Button(top, text="Připojit", width=12, command=self.connect)
        self.btn_connect.pack(side="left")

        self.btn_disconnect = tk.Button(top, text="Odpojit", width=12, command=self.disconnect, state="disabled")
        self.btn_disconnect.pack(side="left", padx=(8, 0))

        self.status = tk.StringVar(value="Status: odpojeno")
        tk.Label(top, textvariable=self.status).pack(side="left", padx=(16, 0))

        self.portinfo = tk.StringVar(value="Port: -")
        tk.Label(root, textvariable=self.portinfo).pack(anchor="w", padx=12)

        grid = tk.Frame(root)
        grid.pack(fill="both", expand=True, padx=12, pady=(10, 12))

        def row(label, var, r):
            tk.Label(grid, text=label, font=("Segoe UI", 11, "bold")).grid(row=r, column=0, sticky="w", pady=4)
            tk.Label(grid, textvariable=var, font=("Consolas", 12)).grid(row=r, column=1, sticky="w", pady=4, padx=12)

        self.v_no  = tk.StringVar(value=self.last_no)
        self.v_nw  = tk.StringVar(value=self.last_nw)
        self.v_uw  = tk.StringVar(value=self.last_uw)
        self.v_pcs = tk.StringVar(value=self.last_pcs)

        row("No.:",  self.v_no,  0)
        row("N.W.:", self.v_nw,  1)
        row("U.W.:", self.v_uw,  2)
        row("PCS.:", self.v_pcs, 3)

        grid.columnconfigure(1, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --- čtení: NON-BLOCKING, takže thread nikdy nevisí ---
    def reader_loop(self):
        while self.running and self.ser:
            try:
                n = self.ser.in_waiting
                if n:
                    data = self.ser.read(n)
                    if data:
                        text = data.decode(errors="ignore")
                        with self.lock:
                            self.buf += text
                            if len(self.buf) > 20000:
                                self.buf = self.buf[-10000:]
                        # parsuj a update hned po přijetí dat
                        self.root.after(0, self.consume_and_update)
                else:
                    time.sleep(0.01)  # lehké uspání, ať to nežere CPU
            except serial.SerialException as exc:
                # Plánuj zpracování na hlavní thread, kde je bezpečné manipulovat s GUI
                self.root.after(0, lambda e=exc: self.handle_serial_error(e))
                break
            except Exception as exc:
                # Neznámé chyby už neututlávejme – převeďme je na viditelnou chybu
                self.root.after(0, lambda e=exc: self.handle_serial_error(e))
                break

    # --- vezmi buffer, naparsuj a update GUI (při každém příjmu dat) ---
    def consume_and_update(self):
        chunk = ""
        with self.lock:
            if self.buf:
                chunk = self.buf
                self.buf = ""

        if not chunk:
            return

        changed = False

        for line in chunk.replace("\r", "\n").split("\n"):
            s = line.strip()
            if not s:
                continue

            m = re_no.match(s)
            if m:
                v = m.group(1)
                if v != self.last_no:
                    self.last_no = v
                    changed = True
                continue

            m = re_nw.match(s)
            if m:
                val, unit = m.group(1), m.group(2)
                v = f"{val}{unit}"
                if v != self.last_nw:
                    self.last_nw = v
                    changed = True
                continue

            m = re_uw.match(s)
            if m:
                val, unit = m.group(1), m.group(2)
                v = f"{val}{unit}"
                if v != self.last_uw:
                    self.last_uw = v
                    changed = True
                continue

            m = re_pcs.match(s)
            if m:
                v = m.group(1)
                if v != self.last_pcs:
                    self.last_pcs = v
                    changed = True
                continue

        if changed:
            self.v_no.set(self.last_no)
            self.v_nw.set(self.last_nw)
            self.v_uw.set(self.last_uw)
            self.v_pcs.set(self.last_pcs)

    def handle_serial_error(self, exc: Exception):
        if not self.running:
            return

        self.running = False
        self.status.set("Status: chyba komunikace")
        self.portinfo.set("Port: -")
        messagebox.showerror("Chyba komunikace", f"Nastala chyba při čtení z portu.\n\n{exc}")
        self.disconnect()

    def connect(self):
        if self.ser:
            return

        port, desc = find_ch340()
        if not port:
            messagebox.showerror("CH340 nenalezen", "Nenašel jsem CH340 v seznamu COM portů.")
            return

        last_err = None
        for _ in range(10):
            try:
                # timeout=0 => non-blocking (klíčové pro čisté zavírání)
                self.ser = serial.Serial(port, BAUD, timeout=0, write_timeout=0)
                # vyčistit buffery
                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except:
                    pass
                break
            except Exception as e:
                last_err = e
                time.sleep(0.3)

        if not self.ser:
            messagebox.showerror("Chyba připojení", f"Nepodařilo se otevřít {port}.\n\n{last_err}")
            return

        self.running = True
        self.thread = threading.Thread(target=self.reader_loop, daemon=True)
        self.thread.start()

        self.status.set("Status: připojeno")
        self.portinfo.set(f"Port: {port} | {desc}")
        self.btn_connect.config(state="disabled")
        self.btn_disconnect.config(state="normal")

    def disconnect(self):
        # 1) zastavit thread a okamžitě odpojit objekt portu od zbytku kódu
        self.running = False
        ser_obj = self.ser
        self.ser = None  # přeruší cyklus reader_loop a umožní znovu otevřít port

        # 2) pokud běží, přeruš čtení a počkej na ukončení
        if ser_obj:
            try:
                ser_obj.cancel_read()
            except Exception:
                pass

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

        # 3) zavřít port co nejtvrději
        if ser_obj:
            try:
                try:
                    ser_obj.reset_input_buffer()
                    ser_obj.reset_output_buffer()
                except Exception:
                    pass
                ser_obj.close()
            except Exception:
                pass

        # 4) uvolnit reference + GC (Windows driver někdy drží handle déle)
        self.thread = None
        with self.lock:
            self.buf = ""

        gc.collect()
        time.sleep(0.8)  # důležité: dej driveru čas uvolnit COM

        self.status.set("Status: odpojeno")
        self.portinfo.set("Port: -")
        self.btn_connect.config(state="normal")
        self.btn_disconnect.config(state="disabled")

    def on_close(self):
        try:
            self.disconnect()
        except:
            pass
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
