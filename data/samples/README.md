# Sample invoices

A curated subset of the synthetic dataset, committed so the examples can be viewed without running the generator. Regenerated automatically by `python src/generate_invoices.py`.

| Document | Ground truth | Template | Vendor (fictional) | Currency | Notes |
|---|---|---|---|---|---|
| [doc_0002.pdf](doc_0002.pdf) | [doc_0002.json](doc_0002.json) | T1_classic | Bluefinch Office Supply Co. | USD |  |
| [doc_0003.pdf](doc_0003.pdf) | [doc_0003.json](doc_0003.json) | T2_modern | Dunecrest Interiors FZ-LLC | AED |  |
| [doc_0022.pdf](doc_0022.pdf) | [doc_0022.json](doc_0022.json) | T3_receipt | Atelier Solenne SARL | EUR |  |
| [doc_0032.pdf](doc_0032.pdf) | [doc_0032.json](doc_0032.json) | T4_two_column | Delta Papyrus Supplies | EGP |  |
| [doc_0045.pdf](doc_0045.pdf) | [doc_0045.json](doc_0045.json) | T5_bilingual_ar_en | Wahat Digital Solutions | SAR | Arabic-Indic digits |
| [doc_0001.png](doc_0001.png) | [doc_0001.json](doc_0001.json) | T5_bilingual_ar_en | Saqr Horizon Facilities | AED | scan |
| [doc_0047.png](doc_0047.png) | [doc_0047.json](doc_0047.json) | T3_receipt | Atelier Solenne SARL | EUR | scan, anomaly; missing_invoice_date: Invoice date is not printed on the document. |
