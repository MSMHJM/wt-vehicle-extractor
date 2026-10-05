# -*- coding: utf-8 -*-
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACTOR = os.path.join(HERE, "wt_extract.py")
VEHICLE_TYPES = {"飞机": "mig_25pd", "坦克": "cn_mbt2000", "军舰": "ship_name", "其它载具": "vehicle_name"}


class ExtractorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("War Thunder 载具工作台")
        self.geometry("1120x760")
        self.minsize(940, 650)
        self.configure(bg="#0b1118")
        self.process = None
        self.log_queue = queue.Queue()
        self.mode = ""
        self.type_var = tk.StringVar(value="飞机")
        self.name_var = tk.StringVar(value=VEHICLE_TYPES["飞机"])
        self.search_var = tk.StringVar()
        self.res_var = tk.StringVar()
        self.blender_var = tk.StringVar()
        self.out_var = tk.StringVar(value="output")
        self.lod_var = tk.StringVar(value="0")
        self.effects_var = tk.BooleanVar(value=False)
        self.keep_temp_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="就绪")
        self.count_var = tk.StringVar(value="未扫描")
        self._build_style()
        self._build_ui()
        self.after(80, self._drain_log)

    def _build_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#0b1118")
        style.configure("Card.TFrame", background="#121b26")
        style.configure("TLabel", background="#0b1118", foreground="#d6e0eb", font=("Segoe UI", 10))
        style.configure("Title.TLabel", background="#0b1118", foreground="#f4f7fb", font=("Segoe UI", 22, "bold"))
        style.configure("Sub.TLabel", background="#0b1118", foreground="#7f91a5", font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background="#121b26", foreground="#f4f7fb", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", padding=(12, 7), font=("Segoe UI", 10))
        style.configure("Accent.TButton", background="#2d9cdb", foreground="white", padding=(18, 9), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#48b5ed"), ("disabled", "#26384b")])
        style.configure("TEntry", fieldbackground="#091018", foreground="#e8f0f7", insertcolor="#e8f0f7")
        style.configure("TCombobox", fieldbackground="#091018", foreground="#e8f0f7")
        style.configure("TCheckbutton", background="#121b26", foreground="#aebdce")

    def _build_ui(self):
        root = ttk.Frame(self, padding=26)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="载具提取工作台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="扫描资源、选择载具、调用 DAE 与 Blender，一次完成模型重建", style="Sub.TLabel").pack(anchor="w", pady=(4, 18))

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True)
        left = ttk.Frame(main, style="Card.TFrame", padding=18)
        left.pack(side="left", fill="y", padx=(0, 14))
        right = ttk.Frame(main, style="Card.TFrame", padding=18)
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="资源与任务", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(left, text="载具类型").pack(anchor="w")
        combo = ttk.Combobox(left, textvariable=self.type_var, values=list(VEHICLE_TYPES), state="readonly", width=22)
        combo.pack(fill="x", pady=(4, 12))
        combo.bind("<<ComboboxSelected>>", self._type_changed)
        ttk.Label(left, text="快速筛选").pack(anchor="w")
        ttk.Entry(left, textvariable=self.search_var).pack(fill="x", pady=(4, 8))
        self.search_var.trace_add("write", lambda *_: self._filter_names())
        self.name_list = tk.Listbox(left, height=22, width=30, bg="#091018", fg="#d9e5ef", selectbackground="#2d9cdb", selectforeground="white", relief="flat", highlightthickness=0, font=("Segoe UI", 10), exportselection=False)
        self.name_list.pack(fill="both", expand=True)
        self.name_list.bind("<<ListboxSelect>>", self._name_selected)
        ttk.Label(left, textvariable=self.count_var, style="Sub.TLabel").pack(anchor="w", pady=(8, 8))
        scan_row = ttk.Frame(left, style="Card.TFrame")
        scan_row.pack(fill="x")
        ttk.Button(scan_row, text="扫描 / 使用缓存", command=self.scan_names).pack(side="left", fill="x", expand=True)
        ttk.Button(scan_row, text="强制刷新", command=lambda: self.scan_names(True)).pack(side="left", padx=(8, 0))

        ttk.Label(right, text="路径设置", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 10))
        self._path_row(right, "游戏 res 目录", self.res_var, self._browse_res)
        self._path_row(right, "Blender 程序", self.blender_var, self._browse_blender)
        self._path_row(right, "输出目录", self.out_var, self._browse_out)
        settings = ttk.Frame(right, style="Card.TFrame")
        settings.pack(fill="x", pady=(10, 14))
        ttk.Label(settings, text="LOD").pack(side="left")
        ttk.Spinbox(settings, from_=0, to=8, textvariable=self.lod_var, width=6).pack(side="left", padx=(8, 18))
        ttk.Checkbutton(settings, text="保留特效面片", variable=self.effects_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(settings, text="保留调试临时文件", variable=self.keep_temp_var).pack(side="left")

        actions = ttk.Frame(right, style="Card.TFrame")
        actions.pack(fill="x", pady=(0, 14))
        self.start_button = ttk.Button(actions, text="开始提取", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止任务", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开输出目录", command=self.open_output).pack(side="left")
        ttk.Button(actions, text="清理缓存", command=self.clear_cache).pack(side="left", padx=8)
        ttk.Button(actions, text="路径诊断", command=self.diagnose).pack(side="left")
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        ttk.Label(right, text="实时日志", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        log_frame = ttk.Frame(right, style="Card.TFrame")
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, bg="#080e14", fg="#b8c8d8", insertbackground="#b8c8d8", relief="flat", font=("Consolas", 9), wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

    def _path_row(self, parent, label, variable, callback):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        ttk.Label(row, text=label, width=13).pack(side="left")
        ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="浏览", command=callback).pack(side="right")

    def _type_changed(self, _event=None):
        self.name_var.set(VEHICLE_TYPES[self.type_var.get()])
        self.name_list.delete(0, "end")
        self.count_var.set("未扫描")
        self.status_var.set("请扫描载具列表")

    def _name_selected(self, _event=None):
        selected = self.name_list.curselection()
        if selected:
            self.name_var.set(self.name_list.get(selected[0]))

    def _filter_names(self):
        query = self.search_var.get().strip().lower()
        values = [n for n in getattr(self, "all_names", []) if query in n.lower()]
        self.name_list.delete(0, "end")
        for name in values:
            self.name_list.insert("end", name)
        self.count_var.set(f"{len(values)} / {len(getattr(self, 'all_names', []))} 个载具")

    def _browse_res(self):
        path = filedialog.askdirectory(title="选择 War Thunder res 目录")
        if path:
            self.res_var.set(path)

    def _browse_blender(self):
        path = filedialog.askopenfilename(title="选择 Blender", filetypes=[("Blender", "blender.exe"), ("所有文件", "*.*")])
        if path:
            self.blender_var.set(path)

    def _browse_out(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.out_var.set(path)

    def _append(self, text):
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def _run(self, cmd, mode):
        if self.process is not None and self.process.poll() is None:
            return
        self.mode = mode
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("扫描中…" if mode == "scan" else "提取中…")
        self.process = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read_process, daemon=True).start()

    def scan_names(self, refresh=False):
        if not os.path.isdir(self.res_var.get()):
            messagebox.showerror("路径错误", "游戏 res 目录不存在，请重新选择。")
            return
        self.all_names = []
        self.name_list.delete(0, "end")
        self.log_text.delete("1.0", "end")
        self._append("正在扫描资源包；首次扫描会建立缓存，之后将按 GRP 文件变化自动失效。\n")
        cmd = [sys.executable, EXTRACTOR, "--list-names", "--category", self.type_var.get(), "--res", self.res_var.get()]
        if refresh:
            cmd.append("--refresh-scan")
        self._run(cmd, "scan")

    def start(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("缺少资源名", "请选择或输入载具资源名。")
            return
        if not os.path.isdir(self.res_var.get()) or not os.path.isfile(self.blender_var.get()):
            messagebox.showerror("路径错误", "请检查游戏 res 目录和 Blender 程序路径。")
            return
        try:
            lod = int(self.lod_var.get())
        except ValueError:
            messagebox.showerror("参数错误", "LOD 必须是整数。")
            return
        out = self.out_var.get().strip() or os.path.join(HERE, "output")
        cmd = [sys.executable, EXTRACTOR, "--name", name, "--res", self.res_var.get(), "--lod", str(lod), "--out", out, "--blender", self.blender_var.get()]
        if self.effects_var.get():
            cmd.append("--keep-effects")
        if self.keep_temp_var.get():
            cmd.append("--keep-temp")
        self.log_text.delete("1.0", "end")
        self._append("$ " + subprocess.list2cmdline(cmd) + "\n\n")
        self._run(cmd, "extract")

    def _read_process(self):
        for line in self.process.stdout:
            self.log_queue.put(line)
        self.log_queue.put(("__DONE__", self.process.wait()))

    def _drain_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    code = item[1]
                    mode = self.mode
                    self.process = None
                    self.mode = ""
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status_var.set("扫描完成" if mode == "scan" and code == 0 else ("完成" if code == 0 else f"失败（退出码 {code}）"))
                    if code == 0 and mode == "extract":
                        messagebox.showinfo("提取完成", "模型已生成，中间文件已按设置清理。")
                elif self.mode == "scan" and item.startswith("__WT_NAME__"):
                    if not hasattr(self, "all_names"):
                        self.all_names = []
                    self.all_names.append(item[len("__WT_NAME__"):].strip())
                    self._filter_names()
                elif not (self.mode == "scan" and item.startswith("[")):
                    self._append(item)
        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.status_var.set("已停止")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def open_output(self):
        path = os.path.abspath(self.out_var.get())
        if os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showwarning("目录不存在", path)

    def clear_cache(self):
        removed = 0
        temp = os.environ.get("TEMP", "")
        if temp and os.path.isdir(temp):
            for name in os.listdir(temp):
                if name.startswith("wt_names_"):
                    path = os.path.join(temp, name)
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
        self.all_names = []
        self.name_list.delete(0, "end")
        self.count_var.set("未扫描")
        self._append(f"已清理 {removed} 个扫描缓存文件。\n")

    def diagnose(self):
        checks = [("提取脚本", os.path.isfile(EXTRACTOR)), ("res 目录", os.path.isdir(self.res_var.get())), ("Blender", os.path.isfile(self.blender_var.get()))]
        self.log_text.delete("1.0", "end")
        self._append("路径诊断\n" + "\n".join(f"{'通过' if ok else '失败'}  {label}: {value}" for label, ok in checks) + "\n")
        self.status_var.set("诊断完成")


if __name__ == "__main__":
    ExtractorApp().mainloop()
