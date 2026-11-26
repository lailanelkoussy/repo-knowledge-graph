package com.acme.vuln.util;

import java.io.InputStream;
import java.io.ObjectInputStream;
import javax.xml.parsers.DocumentBuilderFactory;
import org.w3c.dom.Document;

public final class XmlUtil {

    private XmlUtil() {
    }

    public static Document parseUnsafe(InputStream input) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        return factory.newDocumentBuilder().parse(input);
    }

    public static Document parseSafe(InputStream input) throws Exception {
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        return factory.newDocumentBuilder().parse(input);
    }

    public static Object deserialize(InputStream input) throws Exception {
        return new ObjectInputStream(input).readObject();
    }
}
