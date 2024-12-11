from PyQt5.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel, QTextEdit, \
    QFrame, QDialog
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QFont
import ccxt
import traceback
from settings import KRAKEN_API_KEY, KRAKEN_API_SECRET, GUI_FONT_SIZE, QUICK_SWAP_TICKERS, save_settings, BOOK_UPDATE_THROTTLE
from helpers import format_price, round_to_tick, calculate_adjusted_mid, get_full_symbol, get_user_position, \
    get_open_orders
from datetime import datetime
import websocket
import json
import time
from collections import deque


class WebSocketThread(QThread):
    trade_signal = pyqtSignal(dict)
    last_price_signal = pyqtSignal(float)
    book_signal = pyqtSignal(dict)
    index_signal = pyqtSignal(float)
    error_signal = pyqtSignal()

    def __init__(self, symbol):
        super().__init__()
        self.symbol = symbol
        self.ws = None
        self.running = True
        self.orderbook = {'bids': {}, 'asks': {}}
        self.last_book_update = 0
        self.book_throttle = BOOK_UPDATE_THROTTLE
        self.ping_interval = 30

    def run(self):
        def on_message(ws, message):
            try:
                if not message.startswith('{'):
                    return

                data = json.loads(message)
                feed = data.get('feed')

                if feed == 'trade':
                    if all(key in data for key in ('price', 'qty')):
                        trade = {
                            'time': data.get('time', int(time.time() * 1000)),
                            'side': data.get('side', 'unknown'),
                            'price': float(data.get('price', 0)),
                            'amount': float(data.get('qty', 0))
                        }
                        self.trade_signal.emit(trade)
                        self.last_price_signal.emit(float(data['price']))

                elif feed == 'ticker':
                    if 'markPrice' in data:
                        self.index_signal.emit(float(data['markPrice']))

                elif feed == 'book_snapshot':
                    self.orderbook = {'bids': {}, 'asks': {}}
                    for bid in data.get('bids', []):
                        self.orderbook['bids'][float(bid[0])] = float(bid[1])
                    for ask in data.get('asks', []):
                        self.orderbook['asks'][float(ask[0])] = float(ask[1])
                    self.emit_book_update()

                elif feed == 'book':
                    if all(key in data for key in ('side', 'price', 'qty')):
                        side = 'bids' if data['side'] == 'buy' else 'asks'
                        price = float(data['price'])
                        size = float(data['qty'])

                        if size == 0:
                            self.orderbook[side].pop(price, None)
                        else:
                            self.orderbook[side][price] = size

                        self.emit_book_update()

            except Exception as e:
                print(f"Error processing message: {e}")

        def on_error(ws, error):
            print(f"WebSocket error: {error}")
            self.error_signal.emit()

        def on_close(ws, close_status_code, close_msg):
            print("WebSocket connection closed")
            if self.running:
                self.error_signal.emit()

        def on_open(ws):
            print("WebSocket connection opened")
            subscribe_messages = [
                {
                    "event": "subscribe",
                    "feed": "book",
                    "product_ids": [self.symbol]
                },
                {
                    "event": "subscribe",
                    "feed": "trade",
                    "product_ids": [self.symbol]
                },
                {
                    "event": "subscribe",
                    "feed": "ticker",
                    "product_ids": [self.symbol]
                }
            ]
            for msg in subscribe_messages:
                ws.send(json.dumps(msg))

        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    "wss://futures.kraken.com/ws/v1",
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                    on_open=on_open
                )
                self.ws.run_forever(ping_interval=self.ping_interval)
                if not self.running:
                    break
                time.sleep(5)
            except Exception as e:
                print(f"WebSocket connection error: {e}")
                time.sleep(5)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()

    def emit_book_update(self):
        current_time = time.time() * 1000
        if current_time - self.last_book_update > int(self.book_throttle):
            if self.orderbook['bids'] and self.orderbook['asks']:
                best_bid = max(self.orderbook['bids'].keys())
                best_ask = min(self.orderbook['asks'].keys())
                self.book_signal.emit({
                    'bid': best_bid,
                    'ask': best_ask
                })
                self.last_book_update = current_time


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        from importlib import reload
        import settings
        reload(settings)

        self.setWindowTitle('Settings')
        layout = QVBoxLayout()

        self.ticker_inputs = []
        for i, ticker in enumerate(settings.QUICK_SWAP_TICKERS):
            ticker_layout = QHBoxLayout()
            label = QLabel(f'Quick Swap {i + 1}:')
            ticker_input = QLineEdit()
            ticker_input.setText(ticker)
            self.ticker_inputs.append(ticker_input)
            ticker_layout.addWidget(label)
            ticker_layout.addWidget(ticker_input)
            layout.addLayout(ticker_layout)

        throttle_layout = QHBoxLayout()
        throttle_label = QLabel('Book Update Interval (ms):')
        self.throttle_input = QLineEdit()
        self.throttle_input.setText(str(settings.BOOK_UPDATE_THROTTLE))
        throttle_layout.addWidget(throttle_label)
        throttle_layout.addWidget(self.throttle_input)
        layout.addLayout(throttle_layout)

        save_button = QPushButton('Save')
        save_button.clicked.connect(self.save_and_close)
        layout.addWidget(save_button)
        self.setLayout(layout)

    def save_and_close(self):
        new_tickers = [input_field.text() for input_field in self.ticker_inputs]
        new_throttle = int(self.throttle_input.text())
        save_settings('QUICK_SWAP_TICKERS', new_tickers)
        save_settings('BOOK_UPDATE_THROTTLE', int(new_throttle))
        self.accept()

class DataFetchThread(QThread):
    data_signal = pyqtSignal(dict)
    error_signal = pyqtSignal()

    def __init__(self, exchange, symbol):
        super().__init__()
        self.exchange = exchange
        self.symbol = symbol
        self.running = True

    def run(self):
        while self.running:
            try:
                position = get_user_position(self.exchange, self.symbol)
                open_orders = get_open_orders(self.exchange, self.symbol)
                balance = self.exchange.fetch_balance()
                flex_account = balance['info']['accounts']['flex']
                available_margin = float(flex_account['availableMargin'])
                total_balance = float(flex_account['balanceValue'])

                self.data_signal.emit({
                    'position': position,
                    'open_orders': open_orders,
                    'available_margin': available_margin,
                    'total_balance': total_balance
                })
            except Exception as e:
                print(f"Error fetching data: {str(e)}")
                self.error_signal.emit()

    def stop(self):
        self.running = False


class KrakenTerminal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.light_theme = {
            'background': 'white',
            'text': 'black',
            'button': 'lightgray',
            'window': 'white'
        }
        self.dark_theme = {
            'background': '#2b2b2b',
            'text': '#e0e0e0',
            'button': '#404040',
            'window': '#1e1e1e'
        }
        self.is_dark_mode = False
        self.exchange = ccxt.krakenfutures({
            'apiKey': KRAKEN_API_KEY,
            'secret': KRAKEN_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future'
            }
        })
        self.last_price_label = QLabel()
        self.bid_label = QLabel()
        self.ask_label = QLabel()
        self.mid_label = QLabel()
        self.spread_label = QLabel()
        self.index_price_label = QLabel()
        self.position_label = QLabel()
        self.open_orders_label = QLabel()
        self.order_type = None
        self.tick_size = None
        self.selected_price = None
        self.previous_last_price = None
        self.data_thread = None
        self.ws_thread = None
        self.current_price = None
        self.first_symbol = True
        self.recent_trades = deque(maxlen=1000)
        self.one_minute_volume = 0
        self.one_minute_volume_usd = 0
        self.volume_label = QLabel()
        self.volume_label.setFont(QFont('Arial', GUI_FONT_SIZE))
        self.balance_label = QLabel()
        self.balance_label.setFont(QFont('Arial', GUI_FONT_SIZE))
        self.connection_status_label = QLabel()
        self.connection_status_label.setFixedSize(20, 20)
        self.update_connection_status(False)
        self.is_armed = False
        self.theme_button = QPushButton('🌙')
        self.theme_button.setFixedSize(30, 30)
        self.theme_button.setFont(QFont('Arial', 12))
        self.theme_button.clicked.connect(self.toggle_theme)

        self.margin_requirement = None

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('KrakenFutures Terminal')
        self.setGeometry(100, 100, 600, 100)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)

        default_font = QFont('Arial', GUI_FONT_SIZE)

        quick_swap_layout = QHBoxLayout()
        self.quick_swap_buttons = []
        for i in range(5):
            button = QPushButton('', font=QFont('Arial', GUI_FONT_SIZE - 2))
            button.setFixedHeight(30)
            button.clicked.connect(lambda checked, x=i: self.quick_swap_clicked(x))
            self.quick_swap_buttons.append(button)
            quick_swap_layout.addWidget(button)

        for i, ticker in enumerate(QUICK_SWAP_TICKERS):
            self.quick_swap_buttons[i].setText(ticker)
        self.main_layout.addLayout(quick_swap_layout)

        pair_layout = QHBoxLayout()
        pair_layout.addWidget(self.connection_status_label)
        pair_label = QLabel('Trading Pair:', font=default_font)
        pair_layout.addWidget(pair_label)
        self.pair_input = QLineEdit()
        self.pair_input.setFont(default_font)
        self.pair_input.setText("XBTUSD")
        pair_layout.addWidget(self.pair_input)

        self.confirm_button = QPushButton('Confirm', font=default_font)
        self.confirm_button.clicked.connect(self.on_confirm)
        pair_layout.addWidget(self.confirm_button)
        self.main_layout.addLayout(pair_layout)

        self.hidden_content = QWidget()
        hidden_layout = QVBoxLayout(self.hidden_content)

        order_layout = QVBoxLayout()
        self.usd_value_layout = QHBoxLayout()
        usd_value_label = QLabel('Order Value USD:', font=default_font)
        self.usd_value_layout.addWidget(usd_value_label)
        order_layout.addLayout(self.usd_value_layout)

        button_layout = QHBoxLayout()
        self.buy_button = QPushButton('Buy', font=default_font)
        self.sell_button = QPushButton('Sell', font=default_font)
        self.buy_button.clicked.connect(lambda: self.set_order_type('buy'))
        self.sell_button.clicked.connect(lambda: self.set_order_type('sell'))
        button_layout.addWidget(self.buy_button)
        button_layout.addWidget(self.sell_button)
        order_layout.addLayout(button_layout)

        self.best_price_button = QPushButton("Best", font=default_font)
        self.mid_price_button = QPushButton("Mid", font=default_font)
        self.market_price_button = QPushButton("Market", font=default_font)
        self.price_button = QPushButton("Price", font=default_font)
        self.best_price_button.clicked.connect(self.set_best_price)
        self.mid_price_button.clicked.connect(self.set_mid_price)
        self.market_price_button.clicked.connect(self.set_market_price)
        self.price_button.clicked.connect(self.set_price_input)
        price_layout = QHBoxLayout()
        price_layout.addWidget(self.best_price_button)
        price_layout.addWidget(self.mid_price_button)
        price_layout.addWidget(self.market_price_button)
        price_layout.addWidget(self.price_button)
        order_layout.addLayout(price_layout)

        self.price_input = QLineEdit(font=default_font)
        self.price_input.setPlaceholderText("Enter price")
        self.price_input.textChanged.connect(self.update_selected_price)
        self.price_input.hide()
        order_layout.addWidget(self.price_input)

        quantity_layout = QHBoxLayout()
        quantity_label = QLabel('Quantity:', font=default_font)
        self.volume_input = QLineEdit(font=default_font)
        self.volume_input.textChanged.connect(self.update_usd_value)
        self.pos_button = QPushButton('pos', font=default_font)
        self.pos_button.clicked.connect(self.copy_position_size)
        self.pos_button.hide()
        quantity_layout.addWidget(quantity_label)
        quantity_layout.addWidget(self.volume_input)
        quantity_layout.addWidget(self.pos_button)
        order_layout.addLayout(quantity_layout)

        self.place_order_button = QPushButton('Place Order', font=default_font)
        self.place_order_button.clicked.connect(self.place_order)
        order_layout.addWidget(self.place_order_button)
        hidden_layout.addLayout(order_layout)

        self.close_orders_button = QPushButton('Close All Orders', font=default_font)
        self.close_orders_button.clicked.connect(self.close_all_orders)
        hidden_layout.addWidget(self.close_orders_button)

        self.fast_exit_button = QPushButton('Market Close Position', font=default_font)
        self.fast_exit_button.clicked.connect(self.fast_exit)
        hidden_layout.addWidget(self.fast_exit_button)

        self.data_window = QWidget()
        data_layout = QVBoxLayout(self.data_window)
        for label in [self.last_price_label, self.bid_label, self.mid_label, self.ask_label,
                      self.spread_label, self.index_price_label]:
            label.setFont(QFont('Arial', GUI_FONT_SIZE))
            data_layout.addWidget(label)

        self.volume_label.setFont(QFont('Arial', GUI_FONT_SIZE))
        data_layout.addWidget(self.volume_label)

        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.HLine)
        self.separator.setFrameShadow(QFrame.Sunken)
        self.separator.hide()
        data_layout.addWidget(self.separator)

        self.position_label.setFont(QFont('Arial', GUI_FONT_SIZE))
        self.position_label.setTextFormat(Qt.RichText)
        data_layout.addWidget(self.position_label)

        self.order_separator = QFrame()
        self.order_separator.setFrameShape(QFrame.HLine)
        self.order_separator.setFrameShadow(QFrame.Sunken)
        self.order_separator.hide()
        data_layout.addWidget(self.order_separator)

        self.open_orders_label.setFont(QFont('Arial', GUI_FONT_SIZE))
        data_layout.addWidget(self.open_orders_label)

        self.recent_trades_display = QTextEdit(self)
        self.recent_trades_display.setReadOnly(True)
        self.recent_trades_display.setFont(QFont('Arial', GUI_FONT_SIZE))
        self.recent_trades_display.setFixedHeight(10 * 36)
        self.recent_trades_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        data_layout.addWidget(self.recent_trades_display)

        hidden_layout.addWidget(self.data_window)

        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(self.theme_button)

        self.settings_button = QPushButton('⚙️')
        self.settings_button.setFixedSize(30, 30)
        self.settings_button.setFont(QFont('Arial', 14))
        self.settings_button.clicked.connect(self.open_settings)
        bottom_layout.addWidget(self.settings_button)

        self.arm_button = QPushButton('ARM')
        self.arm_button.setFixedSize(120, 30)
        self.arm_button.setFont(QFont('Arial', 14))
        self.arm_button.clicked.connect(self.toggle_arm)
        self.arm_button.setStyleSheet('background-color: red')
        bottom_layout.addWidget(self.arm_button)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.balance_label)
        hidden_layout.addLayout(bottom_layout)

        self.main_layout.addWidget(self.hidden_content)
        self.hidden_content.hide()

    def quick_swap_clicked(self, button_index):
        if self.quick_swap_buttons[button_index].text():
            self.pair_input.setText(self.quick_swap_buttons[button_index].text())
            self.on_confirm()

    def toggle_theme(self):
        self.is_dark_mode = not self.is_dark_mode
        theme = self.dark_theme if self.is_dark_mode else self.light_theme

        current_style = self.last_price_label.styleSheet()
        if 'color: green' in current_style:
            last_price_color = 'green'
        elif 'color: red' in current_style:
            last_price_color = 'red'
        else:
            last_price_color = theme['text']

        self.last_price_label.setStyleSheet(f'color: {last_price_color}')
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {theme['background']};
                color: {theme['text']};
            }}
            QPushButton {{
                background-color: {theme['button']};
                color: {theme['text']};
                border: 1px solid {theme['text']};
                padding: 5px;
            }}
            QPushButton:disabled {{
                background-color: #1a1a1a;
                color: #666666;
                border: 1px solid #666666;
            }}
            QLineEdit {{
                background-color: {theme['window']};
                color: {theme['text']};
                border: 1px solid {theme['text']};
                padding: 5px;
            }}
            QTextEdit {{
                background-color: {theme['window']};
                color: {theme['text']};
                border: 1px solid {theme['text']};
            }}
            QLabel {{
                color: {theme['text']};
            }}
        """)

        self.theme_button.setText('☀️' if self.is_dark_mode else '🌙')

    def update_connection_status(self, is_connected):
        color = "green" if is_connected else "red"
        self.connection_status_label.setStyleSheet(
            f"background-color: {color}; border-radius: 10px;"
        )

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec_():
            from importlib import reload
            import settings
            reload(settings)

            for i, ticker in enumerate(settings.QUICK_SWAP_TICKERS):
                self.quick_swap_buttons[i].setText(ticker)

            if self.ws_thread:
                self.ws_thread.book_throttle = settings.BOOK_UPDATE_THROTTLE

    def on_confirm(self):
        try:
            symbol = get_full_symbol(self.pair_input.text())

            if symbol:
                if self.data_thread:
                    self.data_thread.stop()
                    self.data_thread.wait()
                if self.ws_thread:
                    self.ws_thread.stop()
                    self.ws_thread.wait()

                self.last_price_label.setText('')
                self.bid_label.setText('')
                self.ask_label.setText('')
                self.mid_label.setText('')
                self.spread_label.setText('')
                self.index_price_label.setText('')
                self.position_label.hide()
                self.open_orders_label.hide()
                self.separator.hide()
                self.order_separator.hide()
                self.volume_label.setText('')
                self.recent_trades_display.clear()
                self.recent_trades = []
                self.one_minute_volume = 0
                self.one_minute_volume_usd = 0
                self.current_price = None
                self.previous_last_price = None
                self.order_type = None
                self.selected_price = None
                self.buy_button.setStyleSheet('')
                self.sell_button.setStyleSheet('')
                self.best_price_button.setStyleSheet('')
                self.mid_price_button.setStyleSheet('')
                self.market_price_button.setStyleSheet('')
                self.price_button.setStyleSheet('')
                self.price_input.hide()
                self.volume_input.clear()
                self.update_usd_value()

                self.exchange.load_markets()
                market = self.exchange.market(symbol)
                self.margin_requirement = float(market['info']['marginLevels'][0]['initialMargin'])
                self.get_tick_size()

                self.data_thread = DataFetchThread(self.exchange, symbol)
                self.data_thread.data_signal.connect(self.update_ui)
                self.data_thread.error_signal.connect(lambda: self.update_connection_status(False))
                self.data_thread.start()

                self.ws_thread = WebSocketThread(symbol)
                self.ws_thread.trade_signal.connect(self.update_recent_trades)
                self.ws_thread.last_price_signal.connect(self.update_last_price)
                self.ws_thread.book_signal.connect(self.update_ticker)
                self.ws_thread.index_signal.connect(self.update_index_price)
                self.ws_thread.error_signal.connect(lambda: self.update_connection_status(False))
                self.ws_thread.start()

                self.hidden_content.show()
                self.update_connection_status(True)
                print(f"Data thread and WebSocket thread started for symbol: {symbol}")

                if symbol:
                    if self.first_symbol:
                        self.setGeometry(100, 100, 800, 600)
                        self.first_symbol = False

        except Exception as e:
            print(f"Error in on_confirm: {str(e)}")
            print(traceback.format_exc())

    def get_tick_size(self):
        try:
            symbol = get_full_symbol(self.pair_input.text())
            market = self.exchange.market(symbol)
            self.tick_size = market['precision']['price']
            self.min_order_size = market['precision']['amount']
            self.volume_input.setPlaceholderText(f"Min: {self.min_order_size}")
            print(f"Tick size for {symbol}: {self.tick_size}")
            print(f"Minimum order size: {self.min_order_size}")
        except Exception as e:
            print(f"Error getting tick size: {str(e)}")
            print(traceback.format_exc())

    def update_ui(self, data):
        try:
            self.setUpdatesEnabled(False)

            position = data['position']
            open_orders = data['open_orders']
            available_margin = data['available_margin']
            total_balance = data['total_balance']

            self.balance_label.setText(f'Free Margin: ${available_margin:.2f} | Balance: ${total_balance:.2f}')

            if position and position['contracts'] != 0:
                entry_price = float(position['entryPrice'])
                quantity = abs(float(position['contracts']))
                position_type = position['info']['side'].upper()
                position_color = 'green' if position_type == "LONG" else 'red'

                if position_type == "LONG":
                    unrealized_pnl = (float(self.ask_label.text().split(': ')[1]) - entry_price) * quantity
                else:
                    unrealized_pnl = (entry_price - float(self.bid_label.text().split(': ')[1])) * quantity

                upnl_percentage = (unrealized_pnl / (entry_price * quantity)) * 100
                fee_rate = 0.00015
                round_trip_fee = entry_price * quantity * fee_rate * 2
                usd_value = entry_price * quantity

                upnl_color = 'green' if unrealized_pnl >= 0 else 'red'
                upnl_text = f"<font color='{upnl_color}'>${format_price(abs(unrealized_pnl))} ({upnl_percentage:.2f}%)</font>"
                upnl_sign = '+' if unrealized_pnl >= 0 else '-'

                position_text = (
                    f"Position: {position_type} | Entry: {format_price(entry_price)}<br>"
                    f"Quantity: <font color='{position_color}'>{quantity}</font> | USD Value: ${format_price(usd_value)}<br>"
                    f"UPNL: {upnl_sign}{upnl_text}<br>"
                    f"Round Trip Fee: ${format_price(round_trip_fee)}"
                )

                self.position_label.setText(position_text)
                self.position_label.show()
                self.separator.show()
                self.pos_button.show()
            else:
                self.position_label.hide()
                self.separator.hide()
                self.pos_button.hide()

            self.update_open_orders_display(open_orders)
            self.update_connection_status(True)

        except Exception as e:
            print(f"Error in update_ui: {str(e)}")
            print(traceback.format_exc())
        finally:
            self.setUpdatesEnabled(True)

    def update_open_orders_display(self, open_orders):
        if open_orders:
            orders_text = "<b>Open Orders:</b><br>"
            for order in open_orders:
                side_color = 'green' if order['side'] == 'buy' else 'red'
                orders_text += f"<font color='{side_color}'>{order['side'].upper()}</font> | Size: {order['amount']} | Price: {format_price(order['price'])}<br>"
            self.open_orders_label.setText(orders_text)
            self.open_orders_label.setTextFormat(Qt.RichText)
            self.open_orders_label.show()
            self.order_separator.show()
        else:
            self.open_orders_label.hide()
            self.order_separator.hide()

    def update_recent_trades(self, trade):
        current_time = time.time()
        if trade['amount'] > 0 and trade['price'] > 0:
            self.recent_trades.append((current_time, trade))

            one_minute_ago = current_time - 60
            valid_trades = [(t, trade) for t, trade in self.recent_trades if t > one_minute_ago]

            volume = sum(trade['amount'] for _, trade in valid_trades)
            volume_usd = sum(trade['amount'] * trade['price'] for _, trade in valid_trades)
            buy_volume = sum(trade['amount'] for _, trade in valid_trades if trade['side'] == 'buy')
            sell_volume = sum(trade['amount'] for _, trade in valid_trades if trade['side'] == 'sell')

            buy_percentage = (buy_volume / volume * 100) if volume > 0 else 0
            sell_percentage = (sell_volume / volume * 100) if volume > 0 else 0

            volume_display = f"{volume / 1000000:.2f}M" if volume >= 1000000 else f"{volume:.4f}"
            self.volume_label.setText(
                f"1M VOL: {volume_display} | ${volume_usd:.2f} (<font color='green'>{buy_percentage:.1f}%</font> / <font color='red'>{sell_percentage:.1f}%</font>)")

            trades_text = ""
            for _, trade in reversed(list(self.recent_trades)[-10:]):
                timestamp = datetime.fromtimestamp(trade['time'] / 1000).strftime('%H:%M:%S')
                color = 'green' if trade['side'] == 'buy' else 'red'
                amount = trade['amount']
                usd_value = amount * trade['price']
                if amount >= 1000000:
                    amount = f"{amount / 1000000:.2f}M"
                trade_line = f"<font color='{color}'><b>{timestamp} | {format_price(trade['price']):<12} | {amount:<8} | ${usd_value:.2f}</b></font><br>"
                trades_text += trade_line
            self.recent_trades_display.setHtml(f"<pre>{trades_text}</pre>")

    def update_volume_display(self):
        current_time = time.time()
        one_minute_ago = current_time - 60

        while self.recent_trades_for_volume and self.recent_trades_for_volume[0][0] < one_minute_ago:
            self.recent_trades_for_volume.popleft()

        self.one_minute_volume = sum(trade[1] for trade in self.recent_trades_for_volume)
        self.one_minute_volume_usd = sum(trade[1] * trade[2] for trade in self.recent_trades_for_volume)

        buy_volume = sum(trade[1] for trade in self.recent_trades_for_volume if trade[3] == 'buy')
        sell_volume = sum(trade[1] for trade in self.recent_trades_for_volume if trade[3] == 'sell')

        buy_percentage = (buy_volume / self.one_minute_volume * 100) if self.one_minute_volume > 0 else 0
        sell_percentage = (sell_volume / self.one_minute_volume * 100) if self.one_minute_volume > 0 else 0

        volume_display = f"{self.one_minute_volume / 1000000:.2f}M" if self.one_minute_volume >= 1000000 else f"{self.one_minute_volume:.4f}"
        self.volume_label.setText(
            f"1M VOL: {volume_display} | ${self.one_minute_volume_usd:.2f} (<font color='green'>{buy_percentage:.1f}%</font> / <font color='red'>{sell_percentage:.1f}%</font>)")

    def update_last_price(self, price):
        self.last_price_label.setText(f'Last: {format_price(price)}')
        self.current_price = price

        if self.previous_last_price:
            if price > self.previous_last_price:
                self.last_price_label.setStyleSheet('color: green')
            elif price < self.previous_last_price:
                self.last_price_label.setStyleSheet('color: red')
            else:
                self.last_price_label.setStyleSheet(
                    f'color: {self.dark_theme["text"] if self.is_dark_mode else self.light_theme["text"]}')
        self.previous_last_price = price

    def update_ticker(self, data):
        bid = data['bid']
        ask = data['ask']
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_percentage = (spread / bid) * 100

        self.bid_label.setText(f'Bid: {format_price(bid)}')
        self.ask_label.setText(f'Ask: {format_price(ask)}')
        self.mid_label.setText(f'Mid: {format_price(mid)}')
        self.spread_label.setText(f'Spread: {format_price(spread)} ({spread_percentage:.2f}%)')

    def update_index_price(self, index_price):
        bid = float(self.bid_label.text().split(': ')[1]) if self.bid_label.text() else 0
        ask = float(self.ask_label.text().split(': ')[1]) if self.ask_label.text() else 0
        mid_price = (bid + ask) / 2 if bid and ask else 0

        premium = mid_price - index_price if mid_price else 0
        premium_percentage = (premium / index_price) * 100 if index_price else 0

        rounded_index = round_to_tick(index_price, self.tick_size)
        rounded_premium = round_to_tick(premium, self.tick_size)

        self.index_price_label.setText(
            f'Index: {format_price(rounded_index)} (Premium: {format_price(rounded_premium)} / {premium_percentage:.2f}%)')

    def set_order_type(self, type):
        if self.order_type != type:
            self.order_type = type
            if type == 'buy':
                self.buy_button.setStyleSheet('background-color: green')
                self.sell_button.setStyleSheet('')
            else:
                self.sell_button.setStyleSheet('background-color: red')
                self.buy_button.setStyleSheet('')

            if self.best_price_button.styleSheet() == 'background-color: blue':
                self.set_best_price()
            elif self.mid_price_button.styleSheet() == 'background-color: blue':
                self.set_mid_price()
            elif self.price_button.styleSheet() == 'background-color: blue':
                if type == 'buy':
                    default_price = self.bid_label.text().split(': ')[1]
                else:
                    default_price = self.ask_label.text().split(': ')[1]
                self.price_input.setText(default_price)
                self.update_selected_price()

            self.update_usd_value()

    def set_best_price(self):
        if self.order_type == 'buy':
            self.selected_price = float(self.bid_label.text().split(': ')[1])
        elif self.order_type == 'sell':
            self.selected_price = float(self.ask_label.text().split(': ')[1])
        print(f"Best price set: {format_price(self.selected_price)}")
        self.best_price_button.setStyleSheet('background-color: blue')
        self.mid_price_button.setStyleSheet('')
        self.market_price_button.setStyleSheet('')
        self.price_button.setStyleSheet('')
        self.price_input.hide()
        self.update_usd_value()

    def set_mid_price(self):
        bid = float(self.bid_label.text().split(': ')[1])
        ask = float(self.ask_label.text().split(': ')[1])
        adjusted_mid = calculate_adjusted_mid(bid, ask, self.tick_size, self.order_type)
        self.selected_price = round_to_tick(adjusted_mid, self.tick_size)
        print(f"Adjusted mid price set: {format_price(self.selected_price)}")
        self.mid_price_button.setStyleSheet('background-color: blue')
        self.best_price_button.setStyleSheet('')
        self.market_price_button.setStyleSheet('')
        self.price_button.setStyleSheet('')
        self.price_input.hide()
        self.update_usd_value()

    def set_market_price(self):
        self.selected_price = None
        print("Market price selected")
        self.market_price_button.setStyleSheet('background-color: blue')
        self.best_price_button.setStyleSheet('')
        self.mid_price_button.setStyleSheet('')
        self.price_button.setStyleSheet('')
        self.price_input.hide()
        self.update_usd_value()

    def set_price_input(self):
        self.selected_price = None
        self.price_button.setStyleSheet('background-color: blue')
        self.best_price_button.setStyleSheet('')
        self.mid_price_button.setStyleSheet('')
        self.market_price_button.setStyleSheet('')
        self.price_input.show()

        if self.order_type == 'buy':
            default_price = self.bid_label.text().split(': ')[1]
        elif self.order_type == 'sell':
            default_price = self.ask_label.text().split(': ')[1]
        else:
            default_price = ''

        self.price_input.setText(default_price)
        self.update_selected_price()
        self.update_usd_value()

    def update_balance(self):
        try:
            balance = self.exchange.fetch_balance()
            flex_account = balance['info']['accounts']['flex']
            available_margin = float(flex_account['availableMargin'])
            total_balance = float(flex_account['balanceValue'])
            self.balance_label.setText(f'Free Margin: ${available_margin:.2f} | Balance: ${total_balance:.2f}')
        except Exception as e:
            print(f"Error fetching balance: {e}")

    def update_selected_price(self):
        try:
            self.selected_price = float(self.price_input.text())
            self.update_usd_value()
        except ValueError:
            pass

    def update_usd_value(self):
        try:
            quantity = float(self.volume_input.text() or 0)
            if self.selected_price:
                price = self.selected_price
            elif self.market_price_button.styleSheet() == 'background-color: blue':
                if self.order_type == 'buy':
                    price = float(self.ask_label.text().split(': ')[1])
                else:
                    price = float(self.bid_label.text().split(': ')[1])
            else:
                bid = float(self.bid_label.text().split(': ')[1])
                ask = float(self.ask_label.text().split(': ')[1])
                price = (bid + ask) / 2

            usd_value = quantity * price
            required_margin = usd_value * self.margin_requirement if self.margin_requirement else 0

            if hasattr(self, 'usd_value_label'):
                self.usd_value_layout.removeWidget(self.usd_value_label)
                self.usd_value_label.deleteLater()

            self.usd_value_label = QLabel(f'${usd_value:.2f} | Margin Cost: ${required_margin:.2f}')
            self.usd_value_label.setFont(QFont('Arial', GUI_FONT_SIZE))
            self.usd_value_layout.addWidget(self.usd_value_label)

        except ValueError:
            if hasattr(self, 'usd_value_label'):
                self.usd_value_layout.removeWidget(self.usd_value_label)
                self.usd_value_label.deleteLater()
            self.usd_value_label = QLabel('$0.00 | Margin Cost: $0.00')
            self.usd_value_label.setFont(QFont('Arial', GUI_FONT_SIZE))
            self.usd_value_layout.addWidget(self.usd_value_label)
        except Exception as e:
            print(f"Error in update_usd_value: {e}")

    def copy_position_size(self):
        try:
            position = get_user_position(self.exchange, get_full_symbol(self.pair_input.text()))
            if position and position['contracts'] != 0:
                quantity = abs(float(position['contracts']))
                self.volume_input.setText(str(quantity))
        except Exception as e:
            print(f"Error copying position size: {str(e)}")

    def toggle_arm(self):
        try:
            self.is_armed = not self.is_armed
            self.arm_button.setText('ARMED' if self.is_armed else 'ARM')
            self.arm_button.setStyleSheet('background-color: green' if self.is_armed else 'background-color: red')

            self.place_order_button.setEnabled(self.is_armed)
            self.close_orders_button.setEnabled(self.is_armed)
            self.fast_exit_button.setEnabled(self.is_armed)
        except Exception as e:
            print(f"Error in toggle_arm: {str(e)}")

    def place_order(self):
        pair = get_full_symbol(self.pair_input.text())
        volume = self.volume_input.text()
        if self.order_type:
            try:
                if self.best_price_button.styleSheet() == 'background-color: blue':
                    self.set_best_price()
                elif self.mid_price_button.styleSheet() == 'background-color: blue':
                    self.set_mid_price()
                elif self.price_button.styleSheet() == 'background-color: blue':
                    self.selected_price = float(self.price_input.text())

                is_market = self.market_price_button.styleSheet() == 'background-color: blue'
                if is_market:
                    order = self.exchange.create_order(
                        symbol=pair,
                        type='market',
                        side=self.order_type,
                        amount=float(volume)
                    )
                else:
                    order = self.exchange.create_order(
                        symbol=pair,
                        type='limit',
                        side=self.order_type,
                        amount=float(volume),
                        price=self.selected_price,
                        params={'postOnly': True}
                    )

                print(
                    f"{'Market' if is_market else 'Limit'} {self.order_type} order placed for {pair}: volume {volume}")
                if not is_market:
                    print(f"Price: {format_price(self.selected_price)}")
                print(f"Order details: {order}")
            except Exception as e:
                print(f"Error placing order: {str(e)}")
        else:
            print("Please select Buy/Sell before placing an order.")

    def close_all_orders(self):
        try:
            open_orders = get_open_orders(self.exchange, get_full_symbol(self.pair_input.text()))
            for order in open_orders:
                self.exchange.cancel_order(order['id'], get_full_symbol(self.pair_input.text()))
            print("All open orders have been closed.")
        except Exception as e:
            print(f"Error closing orders: {str(e)}")

    def fast_exit(self):
        try:
            symbol = get_full_symbol(self.pair_input.text())
            position = get_user_position(self.exchange, symbol)

            if position and position['contracts'] != 0:
                amount = abs(float(position['contracts']))
                side = 'sell' if position['info']['side'].upper() == 'LONG' else 'buy'
                if side == 'sell':
                    order = self.exchange.create_order(
                        symbol=symbol,
                        type='market',
                        side='sell',
                        amount=amount
                    )
                else:
                    order = self.exchange.create_order(
                        symbol=symbol,
                        type='market',
                        side='buy',
                        amount=amount
                    )

                print(f"Fast Exit executed: {side.upper()} {amount} {symbol} at market price")
                print(f"Order details: {order}")
            else:
                print("No open position to exit")
        except Exception as e:
            print(f"Error in fast exit: {str(e)}")
            print(traceback.format_exc())

    def closeEvent(self, event):
        if self.data_thread:
            self.data_thread.stop()
            self.data_thread.wait()
        if self.ws_thread:
            self.ws_thread.stop()
            self.ws_thread.wait()
        event.accept()
