from __future__ import annotations

from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from tgmt5.services.app import AppService, Snapshot
from tgmt5.ui.widgets import hbox, label


class Page(QScrollArea):
    title = ""
    subtitle = ""

    def __init__(self, svc: AppService) -> None:
        super().__init__()
        self.svc = svc
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("Page")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(28, 22, 28, 28)
        self.body.setSpacing(16)
        self.header = hbox(label(self.title, "h1"), "stretch")
        self.body.addLayout(self.header)
        if self.subtitle:
            self.body.addWidget(label(self.subtitle, "muted", wrap=True))
        self.setWidget(inner)
        self.build()

    def build(self) -> None:  # pragma: no cover - overridden
        pass

    def refresh(self, snap: Snapshot) -> None:
        pass

    def on_event(self, kind: str, data: dict) -> None:
        pass

    def activated(self) -> None:
        pass
