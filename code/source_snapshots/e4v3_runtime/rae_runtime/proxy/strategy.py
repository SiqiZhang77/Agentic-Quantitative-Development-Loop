# strategy.py - dummy placeholder for trading strategy
def generate_signals(prices):
    # simple moving average crossover
    short_ma = prices.rolling(10).mean()
    long_ma = prices.rolling(50).mean()
    return (short_ma > long_ma).astype(int)