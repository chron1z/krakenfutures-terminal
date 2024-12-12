from decimal import Decimal, ROUND_HALF_UP


def format_price(price):
    return f"{price:.8f}".rstrip('0').rstrip('.')


def round_to_tick(price, tick_size):
    if tick_size:
        return round(price / tick_size) * tick_size
    return price


def calculate_adjusted_mid(bid, ask, tick_size, order_type):
    bid = Decimal(str(bid))
    ask = Decimal(str(ask))
    mid_price = (bid + ask) / Decimal('2')

    try:
        tick_size_decimal = Decimal(str(tick_size))
    except:
        tick_size_decimal = Decimal('0.00000001')  # Default to 8 decimal places if conversion fails

    rounded_price = mid_price.quantize(tick_size_decimal, rounding=ROUND_HALF_UP)

    if order_type == 'buy':
        rounded_price = min(rounded_price, ask - tick_size_decimal)
    elif order_type == 'sell':
        rounded_price = max(rounded_price, bid + tick_size_decimal)

    return float(rounded_price)


def get_full_symbol(pair):
    return f"PF_{pair}USD"


def get_user_position(exchange, symbol):
    try:
        positions = exchange.fetch_positions()
        if positions:
            return next((p for p in positions if p['info']['symbol'] == symbol), None)
        return None
    except Exception as e:
        print(f"Error fetching position: {str(e)}")
        return None


def get_open_orders(exchange, symbol):
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        return open_orders
    except Exception as e:
        print(f"Error fetching open orders: {str(e)}")
        return []
