-- 既存オブジェクトを全削除(テスト用スキーマ専用。本番では実行しないこと)
BEGIN
  FOR r IN (SELECT object_name, object_type FROM user_objects
             WHERE object_type IN ('VIEW','PACKAGE','FUNCTION','PROCEDURE','SEQUENCE','TABLE')
             ORDER BY DECODE(object_type,'VIEW',1,'PACKAGE',2,'FUNCTION',3,'PROCEDURE',4,'SEQUENCE',5,6)) LOOP
    BEGIN
      IF r.object_type = 'TABLE' THEN
        EXECUTE IMMEDIATE 'DROP TABLE "' || r.object_name || '" CASCADE CONSTRAINTS PURGE';
      ELSE
        EXECUTE IMMEDIATE 'DROP ' || r.object_type || ' "' || r.object_name || '"';
      END IF;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END LOOP;
END;
/
