from src.execution.orders import OrderRequest, place_order

def main():
    # Safe test: 1 share SPY, market order, with a stop loss
    req = OrderRequest(
        symbol="SPY",
        side="BUY",
        quantity=1,
        order_type="MKT",
        stop_loss=490.0
    )
    result = place_order(req)
    print(result)

if __name__ == "__main__":
    main()
