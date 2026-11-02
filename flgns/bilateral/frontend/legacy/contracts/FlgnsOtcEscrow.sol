// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
}

/// @title FLGNS OTC escrow
/// @notice Makers escrow one asset; takers pay and receive the other asset atomically.
///         Makers may cancel; anyone may return expired escrow to its maker.
///         No administrator, upgrade, pause, arbitrary approval, or asset withdrawal function.
/// @dev Adapted from AilgnsOtcEscrow.sol. One deployment per supported TOKEN_B.
contract FlgnsOtcEscrow {
    address public constant FLGNS = 0xffFfBe730E2CdE54D8928D36F547e41DEb66Aaaa;
    address public immutable TOKEN_B;

    address public constant FEE_RECIPIENT_60 = 0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00;
    address public constant FEE_RECIPIENT_40 = 0xEdb9C6900A11d2dd37Da6B59f3604C35A39DB1e6;
    /// @notice 2% of the gross escrow asset received by the taker, rounded down per fill.
    uint256 public constant FEE_BPS = 200;
    uint256 public constant FEE_SHARE_60_BPS = 6_000;
    uint256 public constant MIN_DURATION = 10 minutes;
    uint256 public constant MAX_DURATION = 30 days;

    struct Order {
        address maker;
        bool sellFlgns; // true: deposit FLGNS for TOKEN_B; false: deposit TOKEN_B for FLGNS
        bool open;
        uint64 expiry;
        uint256 giveTotal;
        uint256 wantTotal;
        uint256 giveLeft;
    }

    Order[] private _orders;
    uint256 private _lock = 1;

    event Created(uint256 indexed id, address indexed maker, bool sellFlgns, uint256 give, uint256 want, uint64 expiry);
    event Filled(uint256 indexed id, address indexed taker, uint256 amount, uint256 paid, uint256 fee, uint256 giveLeft);
    event Closed(uint256 indexed id, address indexed maker, uint256 refunded, bool expired);

    modifier nonReentrant() {
        require(_lock == 1, "REENTRANT");
        _lock = 2;
        _;
        _lock = 1;
    }

    constructor(address tokenB) {
        require(
            tokenB == 0x83fd06F0846d9D90B3016bF670Efe2E0B11cDe14 ||
            tokenB == 0x4D1D808a081FdAc440703b3765FC61f8028C06B8 ||
            tokenB == 0x256EdB652db43E721EC6A12B6b51b6967D3bb8cB ||
            tokenB == 0x2e045E1380f5F5b6E7550328910A7377c9FcE59D,
            "UNSUPPORTED_TOKEN_B"
        );
        TOKEN_B = tokenB;
    }

    /// @param sellFlgns true: escrow FLGNS; false: escrow TOKEN_B.
    /// @param duration Seconds until expiry, between 10 minutes and 30 days.
    function create(bool sellFlgns, uint256 give, uint256 want, uint64 duration) external nonReentrant returns (uint256 id) {
        require(give > 0 && want > 0, "ZERO_AMOUNT");
        require(duration >= MIN_DURATION && duration <= MAX_DURATION, "BAD_DURATION");

        address token = sellFlgns ? FLGNS : TOKEN_B;
        _pullExact(token, msg.sender, address(this), give);

        id = _orders.length;
        uint64 expiry = uint64(block.timestamp) + duration;
        _orders.push(Order(msg.sender, sellFlgns, true, expiry, give, want, give));
        emit Created(id, msg.sender, sellFlgns, give, want, expiry);
    }

    /// @notice Pay the maker and receive the escrow asset in the same transaction.
    /// @param amount Gross quantity of the escrow asset; the taker receives amount less 2%.
    /// @param maxPay Maximum payment to the maker, including per-fill price rounding.
    function fill(uint256 id, uint256 amount, uint256 maxPay) external nonReentrant {
        require(id < _orders.length, "NO_ORDER");
        Order storage o = _orders[id];
        require(o.open, "CLOSED");
        require(block.timestamp < o.expiry, "EXPIRED");
        require(amount > 0 && amount <= o.giveLeft, "BAD_AMOUNT");

        uint256 pay = (amount * o.wantTotal + o.giveTotal - 1) / o.giveTotal;
        require(pay <= maxPay, "PRICE_CHANGED");

        o.giveLeft -= amount;
        if (o.giveLeft == 0) o.open = false;

        (address giveToken, address wantToken) = o.sellFlgns ? (FLGNS, TOKEN_B) : (TOKEN_B, FLGNS);
        _pullExact(wantToken, msg.sender, o.maker, pay);

        uint256 fee = (amount * FEE_BPS) / 10_000;
        _pushExact(giveToken, msg.sender, amount - fee);
        if (fee > 0) {
            uint256 fee60 = (fee * FEE_SHARE_60_BPS) / 10_000;
            // The 40% recipient receives every indivisible remainder; no fee stays in escrow.
            if (fee60 > 0) _pushExact(giveToken, FEE_RECIPIENT_60, fee60);
            _pushExact(giveToken, FEE_RECIPIENT_40, fee - fee60);
        }

        emit Filled(id, msg.sender, amount, pay, fee, o.giveLeft);
        if (!o.open) emit Closed(id, o.maker, 0, false);
    }

    function cancel(uint256 id) external nonReentrant {
        require(id < _orders.length, "NO_ORDER");
        require(_orders[id].maker == msg.sender, "NOT_MAKER");
        _close(id, false);
    }

    /// @notice Anyone may trigger expiry refunds; escrow always goes to the original maker.
    function refundExpired(uint256[] calldata ids) external nonReentrant {
        for (uint256 i = 0; i < ids.length; i++) {
            uint256 id = ids[i];
            if (id < _orders.length && _orders[id].open && block.timestamp >= _orders[id].expiry) _close(id, true);
        }
    }

    function _close(uint256 id, bool expired) private {
        Order storage o = _orders[id];
        require(o.open, "CLOSED");
        uint256 left = o.giveLeft;
        o.open = false;
        o.giveLeft = 0;
        if (left > 0) _pushExact(o.sellFlgns ? FLGNS : TOKEN_B, o.maker, left);
        emit Closed(id, o.maker, left, expired);
    }

    function ordersCount() external view returns (uint256) {
        return _orders.length;
    }

    function getOrder(uint256 id) external view returns (Order memory) {
        return _orders[id];
    }

    function getOrders(uint256 from, uint256 count) external view returns (Order[] memory list) {
        uint256 end = from + count;
        if (end > _orders.length) end = _orders.length;
        if (from >= end) return new Order[](0);
        list = new Order[](end - from);
        for (uint256 i = from; i < end; i++) list[i - from] = _orders[i];
    }

    /// @dev Only called with from=msg.sender. Exact receipts reject taxed/abnormal incoming transfers.
    function _pullExact(address token, address from, address to, uint256 amount) private {
        uint256 before = IERC20(token).balanceOf(to);
        _call(token, abi.encodeWithSelector(0x23b872dd, from, to, amount));
        require(IERC20(token).balanceOf(to) - before == amount, "TRANSFER_MISMATCH");
    }

    /// @dev Reject both short receipts and extra escrow debits, including silent transfer taxes.
    ///      Otherwise one order's payout could consume assets backing other open orders.
    function _pushExact(address token, address to, uint256 amount) private {
        uint256 escrowBefore = IERC20(token).balanceOf(address(this));
        uint256 recipientBefore = IERC20(token).balanceOf(to);
        _call(token, abi.encodeWithSelector(0xa9059cbb, to, amount));
        uint256 escrowAfter = IERC20(token).balanceOf(address(this));
        uint256 recipientAfter = IERC20(token).balanceOf(to);
        require(
            escrowAfter <= escrowBefore && escrowBefore - escrowAfter == amount &&
            recipientAfter >= recipientBefore && recipientAfter - recipientBefore == amount,
            "TRANSFER_MISMATCH"
        );
    }

    function _call(address token, bytes memory data) private {
        (bool ok, bytes memory ret) = token.call(data);
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "TOKEN_TRANSFER_FAILED");
    }
}
