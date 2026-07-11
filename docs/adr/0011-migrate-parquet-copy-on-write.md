# Parquet-Schemas werden per Copy-on-write migriert

Eine Parquet-Schema-Migration erzeugt eine neue, vollständig validierte Datensatzversion und aktiviert sie erst durch einen atomaren Manifestwechsel. Veröffentlichte Versionen werden nicht in-place verändert; dadurch bleiben Rollback und Reproduzierbarkeit möglich, bis die vorherige Version bewusst entfernt wird.
