# Game API

## Endpoint

`POST /game`  
`Content-Type: application/json`

---

# Input

## Market

### Example

```json
{
  "positions": [
    {
      "orderType": "MARKET",
      "market": {
        "instrument": "TSLA",
        "direction": "SHORT",
        "createdAt": "2026-03-21T12:00:00Z",
        "takeProfit": 220.0,
        "stopLoss": 320.0,
        "units": 5
      }
    }
  ]
}
```

### Fields

#### Root object

| Field | Type | Required | Description |
|---|---|---:|---|
| `positions` | `array` | yes | List of positions |

#### Position object for `MARKET`

| Field       | Type     | Required | Description          |
| ----------- | -------- | -------: | -------------------- |
| `orderType` | `string` |      yes | Must be `MARKET`     |
| `market`    | `object` |      yes | Market order payload |

#### `market` object

| Field        | Type             | Required | Description       |
| ------------ | ---------------- | -------: | ----------------- |
| `instrument` | `string`         |      yes | Instrument ticker |
| `direction`  | `string`         |      yes | `LONG` or `SHORT` |
| `createdAt`  | `string`         |      yes | RFC3339 datetime  |
| `takeProfit` | `number`         |      yes | Take profit level |
| `stopLoss`   | `number or null` |       no | Stop loss level   |
| `units`      | `number`         |      yes | Position size     |


---

## Limit

### Example

```json
{
  "positions": [
    {
      "orderType": "LIMIT",
      "limit": {
        "instrument": "AAPL",
        "direction": "LONG",
        "createdAt": "2026-03-20T10:00:00Z",
        "takeProfit": 190.0,
        "stopLoss": 175.0,
        "units": 10,
        "limitIn": 180.5
      }
    }
  ]
}
```

### Fields

#### Position object for `LIMIT`

| Field       | Type     | Required | Description         |
| ----------- | -------- | -------: | ------------------- |
| `orderType` | `string` |      yes | Must be `LIMIT`     |
| `limit`     | `object` |      yes | Limit order payload |

#### `limit` object

| Field        | Type             | Required | Description       |
| ------------ | ---------------- | -------: | ----------------- |
| `instrument` | `string`         |      yes | Instrument ticker |
| `direction`  | `string`         |      yes | `LONG` or `SHORT` |
| `createdAt`  | `string`         |      yes | RFC3339 datetime  |
| `takeProfit` | `number`         |      yes | Take profit level |
| `stopLoss`   | `number or null` |       no | Stop loss level   |
| `units`      | `number`         |      yes | Position size     |
| `limitIn`    | `number`         |      yes | Limit entry price |


---

# Response

### Example

```json
{
  "notOpened": [
    {
      "instrument": "AAPL",
      "direction": "LONG",
      "entryType": "LIMIT",
      "createdAt": "2026-03-20T10:00:00Z",
      "limitIn": 180.5,
      "takeProfit": 190.0,
      "stopLoss": 175.0,
      "units": 10
    }
  ],
  "opened": [
    {
      "instrument": "TSLA",
      "direction": "SHORT",
      "entryType": "MARKET",
      "createdAt": "2026-03-21T12:00:00Z",
      "limitIn": null,
      "takeProfit": 220.0,
      "stopLoss": 320.0,
      "units": 5,
      "openPrice": 305.0,
      "openTime": "2026-03-21T13:00:00Z"
    }
  ],
  "closed": [
    {
      "instrument": "MSFT",
      "direction": "LONG",
      "entryType": "LIMIT",
      "createdAt": "2026-03-20T10:00:00Z",
      "limitIn": 410.0,
      "takeProfit": 430.0,
      "stopLoss": 398.0,
      "units": 4,
      "openPrice": 410.0,
      "openTime": "2026-03-20T12:00:00Z",
      "closePrice": 430.0,
      "closeTime": "2026-03-21T09:00:00Z",
      "profit": 80.0,
      "accuracy": "1h"
    }
  ]
}
```

### Fields

#### `closed[]`

| Field        | Type             | Required | Description                    |
| ------------ | ---------------- | -------: | ------------------------------ |
| `instrument` | `string`         |      yes | Instrument ticker              |
| `direction`  | `string`         |      yes | `LONG` or `SHORT`              |
| `entryType`  | `string`         |      yes | `LIMIT` or `MARKET`            |
| `createdAt`  | `string`         |      yes | Creation time                  |
| `limitIn`    | `number or null` |       no | Limit price, null for `MARKET` |
| `takeProfit` | `number`         |      yes | Take profit level              |
| `stopLoss`   | `number or null` |       no | Stop loss level                |
| `units`      | `number`         |      yes | Position size                  |
| `openPrice`  | `number`         |      yes | Actual open price              |
| `openTime`   | `string`         |      yes | Open time                      |
| `closePrice` | `number`         |      yes | Actual close price             |
| `closeTime`  | `string`         |      yes | Close time                     |
| `profit`     | `number`         |      yes | Profit or loss                 |
| `accuracy`   | `string`         |      yes | Candle accuracy                |

#### `opened[]`

| Field        | Type             | Required | Description                    |
| ------------ | ---------------- | -------: | ------------------------------ |
| `instrument` | `string`         |      yes | Instrument ticker              |
| `direction`  | `string`         |      yes | `LONG` or `SHORT`              |
| `entryType`  | `string`         |      yes | `LIMIT` or `MARKET`            |
| `createdAt`  | `string`         |      yes | Creation time                  |
| `limitIn`    | `number or null` |       no | Limit price, null for `MARKET` |
| `takeProfit` | `number`         |      yes | Take profit level              |
| `stopLoss`   | `number or null` |       no | Stop loss level                |
| `units`      | `number`         |      yes | Position size                  |
| `openPrice`  | `number`         |      yes | Actual open price              |
| `openTime`   | `string`         |      yes | Open time                      |

#### `notOpened[]`

| Field        | Type             | Required | Description         |
| ------------ | ---------------- | -------: | ------------------- |
| `instrument` | `string`         |      yes | Instrument ticker   |
| `direction`  | `string`         |      yes | `LONG` or `SHORT`   |
| `entryType`  | `string`         |      yes | `LIMIT` or `MARKET` |
| `createdAt`  | `string`         |      yes | Creation time       |
| `limitIn`    | `number`         |      yes | Limit price         |
| `takeProfit` | `number`         |      yes | Take profit level   |
| `stopLoss`   | `number or null` |       no | Stop loss level     |
| `units`      | `number`         |      yes | Position size       |
