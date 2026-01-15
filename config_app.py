import tkinter as tk
from tkinter import ttk
import cv2
from PIL import Image, ImageTk


class CameraViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Camera Config Viewer")
        self.root.state("zoomed")
        self.caps = []
        self.panels = []

        self.build_ui()
        self.open_cameras()
        self.update_frames()

    def build_ui(self):
        self.container = ttk.Frame(self.root)
        self.container.pack(fill="both", expand=True)

    def enumerate_cameras(self, max_index=16):
        available = []
        for idx in range(max_index):
            cap = cv2.VideoCapture(idx)
            if cap.isOpened():
                available.append(idx)
            cap.release()
        return available

    def open_cameras(self):
        indices = self.enumerate_cameras()
        if not indices:
            ttk.Label(self.container, text="No cameras found.").pack(padx=20, pady=20)
            return

        columns = 2
        for i, idx in enumerate(indices):
            frame = ttk.Frame(self.container)
            row = i // columns
            col = i % columns
            frame.grid(row=row, column=col, padx=10, pady=10, sticky="nsew")
            self.container.grid_columnconfigure(col, weight=1)
            self.container.grid_rowconfigure(row, weight=1)

            label = ttk.Label(frame, text=f"Camera ID: {idx}", anchor="center")
            label.pack()
            panel = ttk.Label(frame)
            panel.pack(fill="both", expand=True)

            cap = cv2.VideoCapture(idx)
            if not cap.isOpened():
                panel.config(text="Unable to open stream.")
                cap.release()
                continue

            self.caps.append(cap)
            self.panels.append(panel)

    def update_frames(self):
        for cap, panel in zip(self.caps, self.panels):
            ret, frame = cap.read()
            if not ret:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            imgtk = ImageTk.PhotoImage(image=img)
            panel.imgtk = imgtk
            panel.config(image=imgtk)
        self.root.after(100, self.update_frames)

    def on_close(self):
        for cap in self.caps:
            cap.release()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = CameraViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
