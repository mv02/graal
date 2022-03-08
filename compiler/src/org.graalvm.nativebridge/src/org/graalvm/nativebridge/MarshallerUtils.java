/*
 * Copyright (c) 2022, Oracle and/or its affiliates. All rights reserved.
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
 *
 * This code is free software; you can redistribute it and/or modify it
 * under the terms of the GNU General Public License version 2 only, as
 * published by the Free Software Foundation.  Oracle designates this
 * particular file as subject to the "Classpath" exception as provided
 * by Oracle in the LICENSE file that accompanied this code.
 *
 * This code is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
 * version 2 for more details (a copy is included in the LICENSE file that
 * accompanied this code).
 *
 * You should have received a copy of the GNU General Public License version
 * 2 along with this work; if not, write to the Free Software Foundation,
 * Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
 *
 * Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
 * or visit www.oracle.com if you need additional information or have any
 * questions.
 */
package org.graalvm.nativebridge;

import java.io.IOException;

public final class MarshallerUtils {

    private MarshallerUtils() {
    }

    public static void writeStackTrace(BinaryOutput out, StackTraceElement[] stack) throws IOException {
        out.writeInt(stack.length);
        for (StackTraceElement stackTraceElement : stack) {
            out.writeUTF(stackTraceElement.getClassName());
            out.writeUTF(stackTraceElement.getMethodName());
            String fileName = stackTraceElement.getFileName();
            out.writeUTF(fileName == null ? "" : fileName);
            out.writeInt(stackTraceElement.getLineNumber());
        }
    }

    public static StackTraceElement[] readStackTrace(BinaryInput in) throws IOException {
        int len = in.readInt();
        StackTraceElement[] res = new StackTraceElement[len];
        for (int i = 0; i < len; i++) {
            String className = in.readUTF();
            String methodName = in.readUTF();
            String fileName = in.readUTF();
            fileName = fileName.isEmpty() ? null : fileName;
            int lineNumber = in.readInt();
            res[i] = new StackTraceElement(className, methodName, fileName, lineNumber);
        }
        return res;
    }
}
