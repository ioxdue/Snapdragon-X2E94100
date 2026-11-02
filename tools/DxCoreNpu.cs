// DXCore COM layout: Microsoft DirectX-Headers, include/directx/dxcore_interface.h.
// Only enumeration is used here; utilization comes from PID-filtered Windows counters.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

namespace LongContext {
    [ComImport, Guid("78ee5945-c36e-4b13-a669-005dd11c0f06"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IFactory {
        [PreserveSig] int CreateAdapterList(uint count, [In] Guid[] attributes, ref Guid iid, out IList list);
    }
    [ComImport, Guid("526c7776-40e9-459b-b711-f32ad76dfc28"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IList {
        [PreserveSig] int GetAdapter(uint index, ref Guid iid, out IAdapter adapter);
        [PreserveSig] uint GetAdapterCount();
    }
    [ComImport, Guid("f0db4c7f-fe5a-42a2-bd62-f2a6cf6fc83e"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAdapter {
        [PreserveSig] [return: MarshalAs(UnmanagedType.I1)] bool IsValid();
        [PreserveSig] [return: MarshalAs(UnmanagedType.I1)] bool IsAttributeSupported(ref Guid attribute);
        [PreserveSig] [return: MarshalAs(UnmanagedType.I1)] bool IsPropertySupported(uint property);
        [PreserveSig] int GetProperty(uint property, UIntPtr size, IntPtr data);
        [PreserveSig] int GetPropertySize(uint property, out UIntPtr size);
    }
    public class Adapter {
        public string Luid;
        public string Description;
        public string DriverVersion;
        public string Identity;
    }
    public static class DxCoreNpu {
        [DllImport("dxcore.dll", ExactSpelling = true)]
        static extern int DXCoreCreateAdapterFactory(ref Guid iid, out IFactory factory);

        static byte[] Property(IAdapter adapter, uint id) {
            if (!adapter.IsPropertySupported(id)) throw new NotSupportedException("DXCore property " + id);
            UIntPtr size;
            Marshal.ThrowExceptionForHR(adapter.GetPropertySize(id, out size));
            int count = checked((int)size.ToUInt64());
            if (count < 1 || count > 65536) throw new InvalidOperationException("Invalid DXCore property size");
            IntPtr buffer = Marshal.AllocHGlobal(count);
            try {
                Marshal.ThrowExceptionForHR(adapter.GetProperty(id, size, buffer));
                byte[] bytes = new byte[count];
                Marshal.Copy(buffer, bytes, 0, count);
                return bytes;
            } finally { Marshal.FreeHGlobal(buffer); }
        }

        public static Adapter[] Enumerate() {
            Guid factoryId = typeof(IFactory).GUID, listId = typeof(IList).GUID, adapterId = typeof(IAdapter).GUID;
            Guid npuType = new Guid("d46140c4-add7-451b-9e56-06fe8c3b58ed");
            Guid compute = new Guid("248e2800-a793-4724-abaa-23a6de1be090");
            var found = new Dictionary<string, Adapter>();
            IFactory factory;
            Marshal.ThrowExceptionForHR(DXCoreCreateAdapterFactory(ref factoryId, out factory));
            try {
                // New systems expose the hardware NPU attribute. The compute list
                // also finds older Hexagon drivers, whose description is retained.
                foreach (Guid attribute in new Guid[] { npuType, compute }) {
                    IList list;
                    int hr = factory.CreateAdapterList(1, new Guid[] { attribute }, ref listId, out list);
                    if (hr < 0) continue;
                    try {
                        for (uint i = 0; i < list.GetAdapterCount(); i++) {
                            IAdapter adapter;
                            Marshal.ThrowExceptionForHR(list.GetAdapter(i, ref adapterId, out adapter));
                            try {
                                if (!adapter.IsValid()) continue;
                                string description = Encoding.UTF8.GetString(Property(adapter, 2)).TrimEnd('\0');
                                bool typed = adapter.IsAttributeSupported(ref npuType);
                                if (!typed && !System.Text.RegularExpressions.Regex.IsMatch(description,
                                    @"\b(NPU|Hexagon|Neural)\b", System.Text.RegularExpressions.RegexOptions.IgnoreCase)) continue;
                                byte[] luid = Property(adapter, 0);
                                string key = "0x" + BitConverter.ToUInt32(luid, 4).ToString("x8") +
                                    "_0x" + BitConverter.ToUInt32(luid, 0).ToString("x8");
                                ulong version = BitConverter.ToUInt64(Property(adapter, 1), 0);
                                found[key] = new Adapter {
                                    Luid = key, Description = description,
                                    DriverVersion = String.Format("{0}.{1}.{2}.{3}", version >> 48,
                                        (version >> 32) & 65535, (version >> 16) & 65535, version & 65535),
                                    Identity = typed ? "DXCore NPU hardware attribute" : "DXCore compute adapter + NPU/Hexagon description"
                                };
                            } finally { Marshal.ReleaseComObject(adapter); }
                        }
                    } finally { Marshal.ReleaseComObject(list); }
                }
            } finally { Marshal.ReleaseComObject(factory); }
            var result = new Adapter[found.Count];
            found.Values.CopyTo(result, 0);
            return result;
        }
    }
}
