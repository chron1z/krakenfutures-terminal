KRAKEN_API_KEY = 'api-key'
KRAKEN_API_SECRET = 'api-secret'
GUI_FONT_SIZE = 22
QUICK_SWAP_TICKERS = ['XBTUSD', 'ETHUSD', 'SOLUSD', 'BONKUSD', 'CRVUSD']


def save_settings(setting_name, value):
    """Save settings to settings.py file"""
    print(f"Attempting to save {setting_name} with value {value}")
    try:
        with open('settings.py', 'r') as file:
            lines = file.readlines()
        print("File read successfully")

        with open('settings.py', 'w') as file:
            found_setting = False
            for line in lines:
                if line.startswith(setting_name):
                    found_setting = True
                    if isinstance(value, list):
                        file.write(f"{setting_name} = {value}\n")
                    else:
                        file.write(f"{setting_name} = '{value}'\n")
                else:
                    file.write(line)

            if not found_setting:
                if isinstance(value, list):
                    file.write(f"\n{setting_name} = {value}\n")
                else:
                    file.write(f"\n{setting_name} = '{value}'\n")
        print("File written successfully")
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False
