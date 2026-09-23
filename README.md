# Handshake

A courier's app says "delivered at 2:14 pm". The customer says nothing came. Today neither side can prove it.

Handshake lets the customer's own Ring camera settle it. When a merchant or courier claims a delivery, the house checks its Ring event history and before/after snapshots for that window, then answers with a signed receipt: delivered, not observed, or inconclusive. The video stays with the house. Only the verdict and the hashes of the evidence leave.

Status: early build for the Build, Ship, Shape: Amazon Developer Hackathon (Ring track).

## Develop

```
python -m pip install -e ".[dev]"
python -m pytest
```

`scripts/probe_playground.py` checks what a Ring Developer Playground token can reach (devices, event history, snapshots, clips).

## License

MIT
