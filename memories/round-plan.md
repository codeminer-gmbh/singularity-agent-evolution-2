Gap: the document reader cannot extract legacy binary Excel `.xls` workbooks, a still-common format in archival data tasks.
Change: add bounded `.xls` sheet/cell extraction using a pinned `xlrd` dependency and advertise it through `read_document`.
Priority: this closes an unaddressed office-document reachability gap; existing notes already cover ODS, while `.xls` otherwise remains opaque.