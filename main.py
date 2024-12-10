import sys
from PyQt5.QtWidgets import QApplication
from gui import KrakenTerminal


def main():
    app = QApplication(sys.argv)
    terminal = KrakenTerminal()
    terminal.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
