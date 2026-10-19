# Canonical schema fixture provenance

`backoffice_canonical_contracts.json` contains all ten version-1 contracts returned by
`backoffice.schemas.catalog.document_schema()` from Back Office backend commit
`d8ce49a47a10d28408c9df05f5514c7c3ee3c52f` (2026-10-19). No document/customer data.

Regenerate in the Back Office environment and copy the JSON here when its contract changes:

```python
import json
from backoffice.schemas.catalog import document_schema
from backoffice.schemas.registry import DOCUMENT_MODELS

contracts = [document_schema(t, v).model_dump(mode="json") for t, v in DOCUMENT_MODELS]
print(json.dumps(contracts, indent=2, ensure_ascii=False))
```

These are compatibility fixtures, not a parser-owned runtime type registry.
