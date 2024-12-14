from PyQt5.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel, QTextEdit, \
    QFrame, QDialog,QSizePolicy, QShortcut, QGridLayout
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QFont, QKeySequence
import ccxt
import traceback
from settings import (KRAKEN_API_KEY, KRAKEN_API_SECRET, GUI_FONT, GUI_FONT_SIZE, QUICK_SWAP_TICKERS,
                      save_settings, BOOK_UPDATE_THROTTLE, PLACE_ORDER_HOTKEY, CLOSE_ORDERS_HOTKEY,
                      CLOSE_LAST_ORDER_HOTKEY, BUY_HOTKEY, SELL_HOTKEY, BEST_PRICE_HOTKEY, PRICE_INPUT_HOTKEY,
                      MARKET_PRICE_HOTKEY, MID_PRICE_HOTKEY)
from helpers import format_price, round_to_tick, calculate_adjusted_mid, get_full_symbol, get_user_position, \
    get_open_orders
from datetime import datetime
import websocket
import json
import time
from collections import deque
import hashlib
import base64
import hmac

class WebSocketThread(QThread):
    trade_signal = pyqtSignal(dict)
    last_price_signal = pyqtSignal(float)
    book_signal = pyqtSignal(dict)
    index_signal = pyqtSignal(float)
    orders_signal = pyqtSignal(list)
    position_signal = pyqtSignal(dict)
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
        self.open_orders = {}

    def sign_challenge(self, challenge):
        challenge_hash = hashlib.sha256(challenge.encode()).digest()
        secret_decoded = base64.b64decode(KRAKEN_API_SECRET)
        signature = hmac.new(secret_decoded, challenge_hash, hashlib.sha512)
        signed_challenge = base64.b64encode(signature.digest()).decode()

        return signed_challenge

    def handle_order_update(self, data):
        if data.get('feed') == 'open_orders_snapshot':
            self.open_orders = {}
            for order in data.get('orders', []):
                if float(order.get('filled', 0)) < float(order.get('qty', 0)):
                    order_id = order.get('order_id')
                    self.open_orders[order_id] = {
                        'id': order_id,
                        'side': 'buy' if order.get('direction') == 0 else 'sell',
                        'qty': float(order.get('qty', 0)),
                        'limitPrice': float(order.get('limit_price', 0)),
                        'filled': float(order.get('filled', 0)),
                        'type': order.get('type'),
                        'reduceOnly': order.get('reduce_only', False),
                        'last_update': order.get('last_update_time')
                    }
        else:
            is_cancel = data.get('is_cancel', False)
            reason = data.get('reason')
            order = data.get('order', {})

            order_id = order.get('order_id') if order else data.get('order_id')

            if is_cancel or (order and float(order.get('filled', 0)) >= float(order.get('qty', 0))):
                if order_id in self.open_orders:
                    del self.open_orders[order_id]
                    print(f"Order {order_id} removed. Reason: {reason}")

            elif order:
                qty = float(order.get('qty', 0))
                filled = float(order.get('filled', 0))
                if filled < qty:
                    self.open_orders[order_id] = {
                        'id': order_id,
                        'side': 'buy' if order.get('direction') == 0 else 'sell',
                        'qty': qty,
                        'limitPrice': float(order.get('limit_price', 0)),
                        'filled': filled,
                        'type': order.get('type'),
                        'reduceOnly': order.get('reduce_only', False),
                        'last_update': order.get('last_update_time')
                    }

        self.orders_signal.emit(list(self.open_orders.values()))

    def run(self):
        def on_message(ws, message):
            try:
                data = json.loads(message)
                # print(f"Processing message type: {data.get('event')} feed: {data.get('feed')} {data}")

                if data.get('event') == 'challenge':
                    challenge = data['message']
                    signed_challenge = self.sign_challenge(challenge)

                    orders_subscription = {
                        "event": "subscribe",
                        "feed": "open_orders",
                        "api_key": KRAKEN_API_KEY,
                        "original_challenge": challenge,
                        "signed_challenge": signed_challenge
                    }
                    # print(f"Sending orders subscription: {orders_subscription}")
                    positions_subscription = {
                        "event": "subscribe",
                        "feed": "open_positions",
                        "api_key": KRAKEN_API_KEY,
                        "original_challenge": challenge,
                        "signed_challenge": signed_challenge
                    }

                    ws.send(json.dumps(orders_subscription))
                    ws.send(json.dumps(positions_subscription))
                    # print(f"Sending orders subscription: {positions_subscription}")

                elif data.get('feed') == 'trade' and data.get('price') and data.get('qty'):
                    if all(key in data for key in ('price', 'qty')):
                        trade = {
                            'time': data.get('time', int(time.time() * 1000)),
                            'side': data.get('side', 'unknown'),
                            'price': float(data.get('price', 0)),
                            'amount': float(data.get('qty', 0))
                        }
                        self.trade_signal.emit(trade)
                        self.last_price_signal.emit(float(data['price']))

                elif data.get('feed') == 'book_snapshot':
                    self.orderbook = {'bids': {}, 'asks': {}}
                    bids = data.get('bids', [])
                    asks = data.get('asks', [])

                    for bid in bids:
                        if isinstance(bid, (list, tuple)) and len(bid) >= 2:
                            self.orderbook['bids'][float(bid[0])] = float(bid[1])

                    for ask in asks:
                        if isinstance(ask, (list, tuple)) and len(ask) >= 2:
                            self.orderbook['asks'][float(ask[0])] = float(ask[1])

                    self.emit_book_update()

                elif data.get('feed') == 'book':
                    if all(key in data for key in ('side', 'price', 'qty')):
                        side = 'bids' if data['side'] == 'buy' else 'asks'
                        price = float(data['price'])
                        size = float(data['qty'])

                        if size == 0:
                            self.orderbook[side].pop(price, None)
                        else:
                            self.orderbook[side][price] = size

                        self.emit_book_update()

                elif data.get('feed') in ['open_orders_snapshot', 'open_orders']:
                    self.handle_order_update(data)

                elif data.get('feed') in ['open_positions', 'open_positions_snapshot']:
                    if len(data.get('positions', [])) > 0:
                        position = data['positions'][0]
                        position_data = {
                            'entryPrice': position['entry_price'],
                            'contracts': position['balance'],
                            'info': {
                                'side': 'LONG' if position['balance'] > 0 else 'SHORT'
                            }
                        }
                        self.position_signal.emit(position_data)
                    else:
                        self.position_signal.emit({})

                elif data.get('feed') == 'ticker':
                    if 'markPrice' in data:
                        index_price = float(data['markPrice'])
                        self.index_signal.emit(index_price)

            except Exception as e:
                print(f"Error in message processing: {str(e)}")
                traceback.print_exc()

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
                # print(f"Sending subscription: {msg}")
                ws.send(json.dumps(msg))

            challenge_request = {
                "event": "challenge",
                "api_key": KRAKEN_API_KEY
            }
            # print(f"Sending challenge request: {challenge_request}")
            ws.send(json.dumps(challenge_request))

        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    "wss://futures.kraken.com/ws/v1",
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close)
                self.ws.on_open = on_open
                self.ws.run_forever()
                if not self.running:
                    break
            except Exception as e:
                print(f"WebSocket connection error: {e}")
                time.sleep(1)

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


class RecentTradesWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Recent Trades')
        layout = QVBoxLayout(self)
        self.trades_display = QTextEdit()
        self.trades_display.setReadOnly(True)
        self.trades_display.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
        self.trades_display.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.trades_display)
        self.resize(850, 360)

    def update_trades(self, trades_text):
        self.trades_display.setHtml(f"<pre>{trades_text}</pre>")

    def clear(self):
        self.trades_display.clear()

class OrdersDisplay(QWidget):
    order_cancelled = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setSpacing(2)
        self.layout.setContentsMargins(0, 0, 0, 0)

    def update_orders(self, orders):
        for i in reversed(range(self.layout.count())):
            self.layout.itemAt(i).widget().setParent(None)

        if orders:
            for order in orders:
                order_widget = QWidget()
                order_layout = QHBoxLayout(order_widget)
                order_layout.setContentsMargins(0, 0, 0, 0)

                side_color = 'green' if order['side'] == 'buy' else 'red'
                text = f"<font color='{side_color}'>{order['side'].upper()}</font> | Size: {order['qty']} | Price: {format_price(order['limitPrice'])}"

                order_label = QLabel(text)
                order_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
                order_label.setTextFormat(Qt.RichText)

                cancel_button = QPushButton("❌")
                cancel_button.setFixedSize(30, 30)
                cancel_button.setStyleSheet("color: red;")

                def create_cancel_handler(order_id):
                    return lambda: self.order_cancelled.emit(order_id)

                cancel_button.clicked.connect(create_cancel_handler(order['id']))

                order_layout.addWidget(order_label)
                order_layout.addWidget(cancel_button)
                order_layout.addStretch()

                self.layout.addWidget(order_widget)

    def create_cancel_handler(self, order_id):
        def handler():
            print(f"Cancel button clicked for order: {order_id}")
            self.order_cancelled.emit(order_id)
        return handler

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

        hotkey_layout = QHBoxLayout()
        place_order_label = QLabel('Place Order Hotkey:')
        self.place_order_input = QLineEdit()
        self.place_order_input.setText(settings.PLACE_ORDER_HOTKEY)

        close_orders_label = QLabel('Close Orders Hotkey:')
        self.close_orders_input = QLineEdit()
        self.close_orders_input.setText(settings.CLOSE_ORDERS_HOTKEY)

        close_last_order_label = QLabel('Close Last Order Hotkey:')
        self.close_last_order_input = QLineEdit()
        self.close_last_order_input.setText(settings.CLOSE_LAST_ORDER_HOTKEY)

        hotkey_layout.addWidget(place_order_label)
        hotkey_layout.addWidget(self.place_order_input)
        hotkey_layout.addWidget(close_orders_label)
        hotkey_layout.addWidget(self.close_orders_input)
        hotkey_layout.addWidget(close_last_order_label)
        hotkey_layout.addWidget(self.close_last_order_input)

        order_hotkeys_layout = QGridLayout()
        order_hotkeys_layout.addWidget(QLabel('Buy Hotkey:'), 0, 0)
        self.buy_hotkey_input = QLineEdit(settings.BUY_HOTKEY)
        order_hotkeys_layout.addWidget(self.buy_hotkey_input, 0, 1)

        order_hotkeys_layout.addWidget(QLabel('Sell Hotkey:'), 1, 0)
        self.sell_hotkey_input = QLineEdit(settings.SELL_HOTKEY)
        order_hotkeys_layout.addWidget(self.sell_hotkey_input, 1, 1)

        order_hotkeys_layout.addWidget(QLabel('Best Price Hotkey:'), 2, 0)
        self.best_price_hotkey_input = QLineEdit(settings.BEST_PRICE_HOTKEY)
        order_hotkeys_layout.addWidget(self.best_price_hotkey_input, 2, 1)

        order_hotkeys_layout.addWidget(QLabel('Mid Price Hotkey:'), 3, 0)
        self.mid_price_hotkey_input = QLineEdit(settings.MID_PRICE_HOTKEY)
        order_hotkeys_layout.addWidget(self.mid_price_hotkey_input, 3, 1)

        order_hotkeys_layout.addWidget(QLabel('Market Price Hotkey:'), 4, 0)
        self.market_price_hotkey_input = QLineEdit(settings.MARKET_PRICE_HOTKEY)
        order_hotkeys_layout.addWidget(self.market_price_hotkey_input, 4, 1)

        order_hotkeys_layout.addWidget(QLabel('Custom Price Hotkey:'), 5, 0)
        self.price_input_hotkey_input = QLineEdit(settings.PRICE_INPUT_HOTKEY)
        order_hotkeys_layout.addWidget(self.price_input_hotkey_input, 5, 1)

        layout.addLayout(order_hotkeys_layout)

        layout.addLayout(hotkey_layout)
        save_button = QPushButton('Save')
        save_button.clicked.connect(self.save_and_close)
        layout.addWidget(save_button)
        self.setLayout(layout)

    def save_and_close(self):
        new_tickers = [input_field.text() for input_field in self.ticker_inputs]
        new_throttle = int(self.throttle_input.text())
        save_settings('QUICK_SWAP_TICKERS', new_tickers)
        save_settings('BOOK_UPDATE_THROTTLE', int(new_throttle))
        save_settings('PLACE_ORDER_HOTKEY', self.place_order_input.text())
        save_settings('CLOSE_ORDERS_HOTKEY', self.close_orders_input.text())
        save_settings('CLOSE_LAST_ORDER_HOTKEY', self.close_last_order_input.text())
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
                balance = self.exchange.fetch_balance()
                flex_account = balance['info']['accounts']['flex']
                available_margin = float(flex_account['availableMargin'])
                total_balance = float(flex_account['balanceValue'])

                self.data_signal.emit({
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
        self.order_type = None
        self.tick_size = None
        self.selected_price = None
        self.previous_last_price = None
        self.data_thread = None
        self.ws_thread = None
        self.current_price = None
        self.first_symbol = True
        self.orderbook = {'bids': {}, 'asks': {}}
        self.recent_trades = deque(maxlen=1000)
        self.one_minute_volume = 0
        self.one_minute_volume_usd = 0
        self.volume_label = QLabel()
        self.volume_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
        self.balance_label = QLabel()
        self.balance_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE - 2))
        self.connection_status_label = QLabel()
        self.connection_status_label.setFixedSize(20, 20)
        self.update_connection_status(False)
        self.is_armed = False
        self.theme_button = QPushButton('🌙')
        self.theme_button.setFixedSize(30, 30)
        self.theme_button.setFont(QFont(GUI_FONT, 12))
        self.theme_button.clicked.connect(self.toggle_theme)
        self.orders_display = OrdersDisplay()
        self.orders_display.order_cancelled.connect(self.cancel_specific_order)
        self.margin_requirement = None
        self.trades_window = RecentTradesWindow()
        self.trades_button = QPushButton('📊')
        self.trades_button.setFixedSize(30, 30)
        self.trades_button.setFont(QFont(GUI_FONT, 14))
        self.trades_button.clicked.connect(self.toggle_trades_window)
        self.last_price_label.setText('Last: waiting for data')
        self.volume_label.setText('1m vol: waiting for data')
        self.init_ui()

    def mousePressEvent(self, event):
        self.setFocus()

    def init_ui(self):
        self.setWindowTitle('KrakenFutures Terminal')
        self.setGeometry(100, 100, 600, 100)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)

        default_font = QFont(GUI_FONT, GUI_FONT_SIZE)

        quick_swap_layout = QHBoxLayout()
        self.quick_swap_buttons = []

        for i in range(5):
            button = QPushButton('', font=QFont(GUI_FONT, GUI_FONT_SIZE - 2))
            button.setFixedHeight(40)
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
        self.pair_input.setText("XBT")
        pair_layout.addWidget(self.pair_input)

        self.confirm_button = QPushButton('Confirm', font=default_font)
        self.confirm_button.clicked.connect(self.on_confirm)
        pair_layout.addWidget(self.confirm_button)
        self.main_layout.addLayout(pair_layout)
        self.pair_input.returnPressed.connect(self.confirm_button.click)

        self.hidden_content = QWidget()
        hidden_layout = QVBoxLayout(self.hidden_content)

        order_layout = QVBoxLayout()
        self.usd_value_layout = QHBoxLayout()
        usd_value_label = QLabel('Order Value:', font=default_font)
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

        self.tick_1_button = QPushButton('1', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))
        self.tick_2_button = QPushButton('2', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))
        self.tick_5_button = QPushButton('5', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4 ))
        self.tick_10_button = QPushButton('10', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4 ))

        for button in [self.tick_1_button, self.tick_2_button, self.tick_5_button, self.tick_10_button]:
            button.setFixedWidth(60)
            button.setFixedHeight(40)

        self.tick_1_button.clicked.connect(lambda: self.adjust_price_by_ticks(1))
        self.tick_2_button.clicked.connect(lambda: self.adjust_price_by_ticks(2))
        self.tick_5_button.clicked.connect(lambda: self.adjust_price_by_ticks(5))
        self.tick_10_button.clicked.connect(lambda: self.adjust_price_by_ticks(10))

        self.price_input = QLineEdit(font=QFont(GUI_FONT, GUI_FONT_SIZE))
        self.price_input.setPlaceholderText("Enter price")
        self.price_input.textChanged.connect(self.update_selected_price)
        self.price_input.hide()

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

        self.price_input_container = QWidget()
        price_input_layout = QHBoxLayout(self.price_input_container)
        price_input_layout.setSpacing(2)
        price_input_layout.setContentsMargins(0, 0, 0, 0)
        price_input_layout.addWidget(self.tick_1_button)
        price_input_layout.addWidget(self.tick_2_button)
        price_input_layout.addWidget(self.tick_5_button)
        price_input_layout.addWidget(self.tick_10_button)
        price_input_layout.addWidget(self.price_input)
        self.price_input_container.hide()
        order_layout.addWidget(self.price_input_container)

        quantity_container = QWidget()
        quantity_layout = QHBoxLayout(quantity_container)
        quantity_layout.setSpacing(2)
        quantity_layout.setContentsMargins(0, 0, 0, 0)

        self.volume_input = QLineEdit(font=QFont(GUI_FONT, GUI_FONT_SIZE))
        self.volume_input.setMinimumWidth(300)
        self.volume_input.textChanged.connect(self.update_usd_value)
        self.volume_input.editingFinished.connect(self.enforce_min_size_multiple)

        self.qty_1_button = QPushButton('1', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))
        self.qty_10_button = QPushButton('10', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))
        self.qty_100_button = QPushButton('100', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))
        self.qty_1000_button = QPushButton('1000', font=QFont(GUI_FONT, GUI_FONT_SIZE - 4))

        for button in [self.qty_1_button, self.qty_10_button, self.qty_100_button, self.qty_1000_button]:
            button.setFixedWidth(60)
            button.setFixedHeight(40)
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.qty_1_button.clicked.connect(lambda: self.adjust_quantity(1))
        self.qty_10_button.clicked.connect(lambda: self.adjust_quantity(10))
        self.qty_100_button.clicked.connect(lambda: self.adjust_quantity(100))
        self.qty_1000_button.clicked.connect(lambda: self.adjust_quantity(1000))

        self.clear_qty_button = QPushButton('×', font=QFont(GUI_FONT, GUI_FONT_SIZE))
        self.clear_qty_button.setFixedWidth(25)
        self.clear_qty_button.setFixedHeight(45)
        self.clear_qty_button.setStyleSheet('background-color: red; color: white;')
        self.clear_qty_button.clicked.connect(lambda: self.volume_input.setText(''))

        quantity_layout.addWidget(self.qty_1_button)
        quantity_layout.addWidget(self.qty_10_button)
        quantity_layout.addWidget(self.qty_100_button)
        quantity_layout.addWidget(self.qty_1000_button)
        quantity_layout.addWidget(self.volume_input)
        quantity_layout.addWidget(self.clear_qty_button)

        self.pos_button = QPushButton('pos', font=default_font)
        self.pos_button.clicked.connect(self.copy_position_size)
        self.pos_button.hide()

        quantity_layout.addWidget(self.pos_button)
        order_layout.addWidget(quantity_container)
        order_layout.addLayout(quantity_layout)

        self.place_order_button = QPushButton('Place Order', font=default_font)
        self.place_order_button.clicked.connect(self.place_order)
        order_layout.addWidget(self.place_order_button)
        hidden_layout.addLayout(order_layout)

        close_buttons_layout = QHBoxLayout()
        self.close_orders_button = QPushButton('Close All Orders', font=default_font)
        self.close_orders_button.clicked.connect(self.close_all_orders)
        self.close_last_order_button = QPushButton('Close Last Order', font=default_font)
        self.close_last_order_button.clicked.connect(self.close_last_order)
        close_buttons_layout.addWidget(self.close_orders_button)
        close_buttons_layout.addWidget(self.close_last_order_button)
        hidden_layout.addLayout(close_buttons_layout)

        self.fast_exit_button = QPushButton('Market Close Position', font=default_font)
        self.fast_exit_button.clicked.connect(self.fast_exit)
        hidden_layout.addWidget(self.fast_exit_button)

        self.data_window = QWidget()
        data_layout = QVBoxLayout(self.data_window)
        for label in [self.last_price_label, self.bid_label, self.mid_label, self.ask_label,
                      self.spread_label, self.index_price_label]:
            label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
            label.setMinimumWidth(50)
            label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            data_layout.addWidget(label)
        self.volume_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
        data_layout.addWidget(self.volume_label)

        data_order_separator = QFrame()
        data_order_separator.setFrameShape(QFrame.HLine)
        data_order_separator.setFrameShadow(QFrame.Sunken)
        data_order_separator.setStyleSheet("background-color: #404040; margin: 5px 0px;")
        data_order_separator.setFixedHeight(1)
        data_layout.addWidget(data_order_separator)

        self.position_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
        self.position_label.setTextFormat(Qt.RichText)
        data_layout.addWidget(self.position_label)

        self.separator = QFrame()
        self.separator.setFrameShape(QFrame.HLine)
        self.separator.setFrameShadow(QFrame.Sunken)
        self.separator.hide()
        data_layout.addWidget(self.separator)

        self.order_separator = QFrame()
        self.order_separator.setFrameShape(QFrame.HLine)
        self.order_separator.setFrameShadow(QFrame.Sunken)
        self.order_separator.hide()
        data_layout.addWidget(self.order_separator)
        data_layout.addWidget(self.orders_display)

        hidden_layout.addWidget(self.data_window)

        bottom_layout = QHBoxLayout()
        bottom_layout.addWidget(self.theme_button)

        self.settings_button = QPushButton('⚙️')
        self.settings_button.setFixedSize(30, 30)
        self.settings_button.setFont(QFont(GUI_FONT, 14))
        self.settings_button.clicked.connect(self.open_settings)
        bottom_layout.addWidget(self.settings_button)
        bottom_layout.addWidget(self.trades_button)

        self.place_order_shortcut = QShortcut(QKeySequence(PLACE_ORDER_HOTKEY), self)
        self.place_order_shortcut.activated.connect(
            lambda: self.place_order() if self.ws_thread and self.is_armed else None)

        self.close_orders_shortcut = QShortcut(QKeySequence(CLOSE_ORDERS_HOTKEY), self)
        self.close_orders_shortcut.activated.connect(
            lambda: self.close_all_orders() if self.ws_thread and self.is_armed else None)

        self.close_last_order_shortcut = QShortcut(QKeySequence(CLOSE_LAST_ORDER_HOTKEY), self)
        self.close_last_order_shortcut.activated.connect(
            lambda: self.close_last_order() if self.ws_thread and self.is_armed else None)

        self.buy_shortcut = QShortcut(QKeySequence("Alt+1"), self)
        self.buy_shortcut.activated.connect(
            lambda: self.set_order_type('buy') if self.ws_thread else None)

        self.sell_shortcut = QShortcut(QKeySequence("Alt+2"), self)
        self.sell_shortcut.activated.connect(
            lambda: self.set_order_type('sell') if self.ws_thread else None)

        self.best_price_shortcut = QShortcut(QKeySequence("Shift+1"), self)
        self.best_price_shortcut.activated.connect(
            lambda: self.set_best_price() if self.ws_thread and self.order_type else None)

        self.mid_price_shortcut = QShortcut(QKeySequence("Shift+2"), self)
        self.mid_price_shortcut.activated.connect(
            lambda: self.set_mid_price() if self.ws_thread and self.order_type else None)

        self.market_price_shortcut = QShortcut(QKeySequence("Shift+3"), self)
        self.market_price_shortcut.activated.connect(
            lambda: self.set_market_price() if self.ws_thread and self.order_type else None)

        self.price_input_shortcut = QShortcut(QKeySequence("Shift+4"), self)
        self.price_input_shortcut.activated.connect(
            lambda: self.set_price_input() if self.ws_thread and self.order_type else None)

        self.arm_button = QPushButton('ARM')
        self.arm_button.setFixedSize(120, 30)
        self.arm_button.setFont(QFont(GUI_FONT, 14))
        self.arm_button.clicked.connect(self.toggle_arm)
        self.arm_button.setStyleSheet('background-color: red')
        bottom_layout.addWidget(self.arm_button)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.balance_label)
        hidden_layout.addStretch()
        hidden_layout.addLayout(bottom_layout)

        self.main_layout.addWidget(self.hidden_content)
        self.hidden_content.hide()

        self.is_armed = False
        self.place_order_button.setEnabled(False)
        self.close_orders_button.setEnabled(False)
        self.fast_exit_button.setEnabled(False)
        self.close_last_order_button.setEnabled(False)
        self.close_last_order_button.setStyleSheet('background-color: #1a1a1a')
        self.place_order_button.setStyleSheet('background-color: #1a1a1a')
        self.close_orders_button.setStyleSheet('background-color: #1a1a1a')
        self.fast_exit_button.setStyleSheet('background-color: #1a1a1a')

    def close_last_order(self):
        print('Attempting to close last order.')

        try:
            orders = list(self.ws_thread.open_orders.values())
            if orders:
                last_order = orders[-1]
                self.exchange.cancel_order(last_order['id'], get_full_symbol(self.pair_input.text()))
                print(f"Last order {last_order['id']} has been closed.")
        except Exception as e:
            print(f"Error closing last order: {str(e)}")
    def toggle_trades_window(self):
        if self.trades_window.isVisible():
            self.trades_window.hide()
        else:
            self.trades_window.show()

    def adjust_price_by_ticks(self, num_ticks):
        if not self.order_type or not self.price_input.text():
            return

        current_price = float(self.price_input.text())
        adjustment = self.tick_size * num_ticks

        if self.order_type == 'sell':
            new_price = current_price + adjustment
        else:
            new_price = current_price - adjustment

        formatted_price = format_price(round_to_tick(new_price, self.tick_size))
        self.price_input.setText(formatted_price)

    def adjust_quantity(self, multiplier):
        try:
            current_qty = float(self.volume_input.text() or 0)
            increment = self.min_order_size * multiplier
            new_qty = current_qty + increment
            self.volume_input.setText(str(new_qty))

        except ValueError:
            self.volume_input.setText(str(self.min_order_size * multiplier))

    def cancel_specific_order(self, order_id):
        print(f"Attempting to cancel order: {order_id}")
        try:
            self.exchange.cancel_order(order_id, get_full_symbol(self.pair_input.text()))
            print(f"Order {order_id} cancelled successfully")
        except Exception as e:
            print(f"Error cancelling order: {str(e)}")

    def quick_swap_clicked(self, button_index):
        if self.quick_swap_buttons[button_index].text():
            self.pair_input.setText(self.quick_swap_buttons[button_index].text())
            self.on_confirm()

    def enforce_min_size_multiple(self):
        try:
            current_qty = float(self.volume_input.text() or 0)
            closest_multiple = round(current_qty / self.min_order_size) * self.min_order_size
            self.volume_input.setText(str(closest_multiple))
        except ValueError:
            pass

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
            new_symbol = get_full_symbol(self.pair_input.text())

            current_symbol = self.ws_thread.symbol if self.ws_thread else None

            if new_symbol == current_symbol:
                return

            if symbol:
                if self.data_thread:
                    self.data_thread.stop()
                    self.data_thread.wait()
                if self.ws_thread:
                    self.ws_thread.stop()
                    self.ws_thread.wait()

                self.last_price_label.setText('Last: waiting...')
                self.bid_label.setText('')
                self.ask_label.setText('')
                self.mid_label.setText('')
                self.spread_label.setText('')
                self.index_price_label.setText('')
                self.volume_label.setText('1m vol: waiting...')
                self.position_label.hide()
                self.separator.hide()
                self.order_separator.hide()
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
                self.price_input_container.hide()
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
                self.trades_window.clear()
                self.recent_trades = []

                position = get_user_position(self.exchange, symbol)
                open_orders = get_open_orders(self.exchange, symbol)
                self.update_ui({
                    'position': position,
                    'open_orders': open_orders
                })

                self.ws_thread = WebSocketThread(symbol)
                self.ws_thread.trade_signal.connect(self.update_recent_trades)
                self.ws_thread.last_price_signal.connect(self.update_last_price)
                self.ws_thread.book_signal.connect(self.update_ticker)
                self.ws_thread.orders_signal.connect(self.orders_display.update_orders)
                self.ws_thread.index_signal.connect(self.update_index_price)
                self.ws_thread.error_signal.connect(lambda: self.update_connection_status(False))
                self.ws_thread.position_signal.connect(self.update_position_display)
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
            self.volume_input.setPlaceholderText(f"Min size: {self.min_order_size}")
            print(f"Tick size for {symbol}: {self.tick_size}")
            print(f"Minimum order size: {self.min_order_size}")
        except Exception as e:
            print(f"Error getting tick size: {str(e)}")
            print(traceback.format_exc())

    def calculate_impact_price(self, size, side):
        """Calculate average execution price for market order of given size"""
        total_cost = 0
        remaining_size = size
        book_side = 'bids' if side == 'sell' else 'asks'
        prices = sorted(self.orderbook[book_side].keys(), reverse=(book_side == 'bids'))

        for price in prices:
            level_size = self.orderbook[book_side][price]
            executed = min(remaining_size, level_size)
            total_cost += executed * price
            remaining_size -= executed

            if remaining_size <= 0:
                break

        return total_cost / size

    def update_ui(self, data):
        try:
            available_margin = data['available_margin']
            total_balance = data['total_balance']
            self.balance_label.setText(f'Margin: ${available_margin:.2f} | Balance: ${total_balance:.2f}')
            self.update_connection_status(True)
            self.update_usd_value()
        except Exception as e:
            print(f"Error in update_ui: {str(e)}")


    def update_position_display(self, data):
        if data is None:
            self.position_label.hide()
            self.separator.hide()
            self.pos_button.hide()
            return
        try:
            if data and 'entryPrice' in data:
                entry_price = float(data['entryPrice'])
                quantity = abs(float(data['contracts']))
                position_type = data['info']['side']

                bid = float(self.bid_label.text().split(': ')[1]) if self.bid_label.text() else 0
                ask = float(self.ask_label.text().split(': ')[1]) if self.ask_label.text() else 0
                mid_price = (bid + ask) / 2

                if position_type == "LONG":
                    mid_pnl = (mid_price - entry_price) * quantity
                    best_pnl = (ask - entry_price) * quantity
                    impact_price = self.calculate_impact_price(quantity, 'sell')
                    impact_pnl = (impact_price - entry_price) * quantity
                else:
                    mid_pnl = (entry_price - mid_price) * quantity
                    best_pnl = (entry_price - bid) * quantity
                    impact_price = self.calculate_impact_price(quantity, 'buy')
                    impact_pnl = (entry_price - impact_price) * quantity

                mid_percentage = (mid_pnl / (entry_price * quantity)) * 100
                best_percentage = (best_pnl / (entry_price * quantity)) * 100
                impact_percentage = (impact_pnl / (entry_price * quantity)) * 100

                mid_color = 'green' if mid_pnl >= 0 else 'red'
                best_color = 'green' if best_pnl >= 0 else 'red'
                impact_color = 'green' if impact_pnl >= 0 else 'red'
                position_color = 'green' if position_type == "LONG" else 'red'

                position_text = (
                    f"Position: {position_type} | Entry: {format_price(entry_price)} | "
                    f"Quantity: <font color='{position_color}'>{quantity}</font> | Value: ${format_price(entry_price * quantity)}<br>"
                    f"UPNL: MID <font color='{mid_color}'>${format_price(abs(mid_pnl))} ({mid_percentage:.2f}%)</font> | "
                    f"BEST <font color='{best_color}'>${format_price(abs(best_pnl))} ({best_percentage:.2f}%)</font> | "
                    f"MARKET <font color='{impact_color}'>${format_price(abs(impact_pnl))} ({impact_percentage:.2f}%)</font>"
                )

                self.position_label.setText(position_text)
                self.position_label.show()
                self.separator.show()
                self.pos_button.show()
            else:
                self.position_label.hide()
                self.separator.hide()
                self.pos_button.hide()
        except Exception as e:
            print(f"Error in update_position_display: {str(e)}")

    def format_volume_usd(self, volume_usd):
        if volume_usd >= 1000000:
            return f"${volume_usd / 1000000:.2f}M"
        elif volume_usd >= 1000:
            return f"${volume_usd / 1000:.2f}K"
        return f"${volume_usd:.2f}"

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
            formatted_usd = self.format_volume_usd(volume_usd)
            self.volume_label.setText(
                f"1M VOL: {volume_display} | {formatted_usd} (<font color='green'>{buy_percentage:.1f}%</font> / <font color='red'>{sell_percentage:.1f}%</font>)")
            trades_text = ""
            for _, trade in reversed(list(self.recent_trades)[-10:]):
                timestamp = datetime.fromtimestamp(trade['time'] / 1000).strftime('%H:%M:%S')
                color = '#00B300' if trade['side'] == 'buy' else 'red'
                amount = trade['amount']
                usd_value = amount * trade['price']
                if amount >= 1000000:
                    amount = f"{amount / 1000000:.2f}M"
                trade_line = f"<font color='{color}'><b>{timestamp} | {format_price(trade['price']):<12} | {amount:<8} | ${usd_value:.2f}</b></font><br>"
                trades_text += trade_line

            self.trades_window.update_trades(trades_text)

    def update_volume_display(self):
        current_time = time.time()
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
        self.orderbook = self.ws_thread.orderbook

        self.update_usd_value()

    def update_index_price(self, index_price):
        if not self.bid_label.text() or not self.ask_label.text():
            return

        bid = float(self.bid_label.text().split(': ')[1])
        ask = float(self.ask_label.text().split(': ')[1])
        mid_price = (bid + ask) / 2

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
        self.price_input_container.hide()
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
        self.price_input_container.hide()
        self.update_usd_value()

    def set_market_price(self):
        self.selected_price = None
        print("Market price selected")
        self.market_price_button.setStyleSheet('background-color: blue')
        self.best_price_button.setStyleSheet('')
        self.mid_price_button.setStyleSheet('')
        self.price_button.setStyleSheet('')
        self.price_input_container.hide()
        self.update_usd_value()

    def set_price_input(self):
        self.selected_price = None
        self.price_button.setStyleSheet('background-color: blue')
        self.best_price_button.setStyleSheet('')
        self.mid_price_button.setStyleSheet('')
        self.market_price_button.setStyleSheet('')

        self.price_input_container.show()
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
            entered_price = float(self.price_input.text())
            bid = float(self.bid_label.text().split(': ')[1])
            ask = float(self.ask_label.text().split(': ')[1])

            if self.order_type == 'buy':
                max_price = ask - self.tick_size
                if entered_price > max_price:
                    self.price_input.setText(format_price(max_price))
            elif self.order_type == 'sell':
                min_price = bid + self.tick_size
                if entered_price < min_price:
                    self.price_input.setText(format_price(min_price))

            self.selected_price = float(self.price_input.text())
            self.update_usd_value()
        except ValueError:
            pass

    def update_usd_value(self):
        try:
            quantity = float(self.volume_input.text() or 0)

            bid = float(self.bid_label.text().split(': ')[1]) if self.bid_label.text() else 0
            ask = float(self.ask_label.text().split(': ')[1]) if self.ask_label.text() else 0
            mid = (bid + ask) / 2

            if self.selected_price:
                price = self.selected_price
            elif self.best_price_button.styleSheet() == 'background-color: blue':
                if self.order_type == 'buy':
                    price = bid
                elif self.order_type == 'sell':
                    price = ask
                else:
                    price = mid
            else:
                price = mid


            usd_value = quantity * price
            required_margin = usd_value * float(self.margin_requirement) if self.margin_requirement else 0

            formatted_text = f'<b>${usd_value:12.2f} | Margin Cost: ${required_margin:12.2f}</b>'

            if hasattr(self, 'usd_value_label'):
                self.usd_value_label.setText(formatted_text)
            else:
                self.usd_value_label = QLabel(formatted_text)
                self.usd_value_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
                self.usd_value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                self.usd_value_label.setAlignment(Qt.AlignLeft)
                self.usd_value_layout.addWidget(self.usd_value_label)

        except ValueError:
            formatted_text = '<b>$0.00            | Margin Cost: $0.00           </b>'
            if hasattr(self, 'usd_value_label'):
                self.usd_value_label.setText(formatted_text)
            else:
                self.usd_value_label = QLabel(formatted_text)
                self.usd_value_label.setFont(QFont(GUI_FONT, GUI_FONT_SIZE))
                self.usd_value_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
                self.usd_value_label.setAlignment(Qt.AlignLeft)
                self.usd_value_layout.addWidget(self.usd_value_label)

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
            self.close_last_order_button.setEnabled(self.is_armed)
            self.fast_exit_button.setEnabled(self.is_armed)

            if not self.is_armed:
                self.place_order_button.setStyleSheet('background-color: #1a1a1a')
                self.close_orders_button.setStyleSheet('background-color: #1a1a1a')
                self.close_last_order_button.setStyleSheet('background-color: #1a1a1a')
                self.fast_exit_button.setStyleSheet('background-color: #1a1a1a')
            else:
                self.place_order_button.setStyleSheet('')
                self.close_orders_button.setStyleSheet('')
                self.close_last_order_button.setStyleSheet('')
                self.fast_exit_button.setStyleSheet('')

        except Exception as e:
            print(f"Error in toggle_arm: {str(e)}")

    def place_order(self):
        pair = get_full_symbol(self.pair_input.text())
        volume = self.volume_input.text()
        if self.order_type:
            try:
                current_qty = float(self.volume_input.text() or 0)
                adjusted_qty = round(current_qty / self.min_order_size) * self.min_order_size
                self.volume_input.setText(str(adjusted_qty))

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
            orders = list(self.ws_thread.open_orders.values())
            for order in orders:
                self.exchange.cancel_order(order['id'], get_full_symbol(self.pair_input.text()))
                print(f"Order {order['id']} cancelled")
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
