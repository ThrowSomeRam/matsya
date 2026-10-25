"""Invoice adapters by name, as a manifest or an upload names them."""

from matsya_platform.ingest.gst_einvoice import GstEInvoiceAdapter
from matsya_platform.ingest.invoices import InvoiceAdapter
from matsya_platform.ingest.invoices_csv import GenericCsvAdapter
from matsya_platform.ingest.tally_daybook import TallyDayBookAdapter

ADAPTERS: dict[str, type[InvoiceAdapter]] = {a.name: a for a in (GenericCsvAdapter, TallyDayBookAdapter,
                                                                  GstEInvoiceAdapter)}


def adapter(name: str) -> InvoiceAdapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise KeyError(f"no invoice adapter {name!r}; known: {sorted(ADAPTERS)}") from None
