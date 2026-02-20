import streamlit as st
import pandas as pd
import numpy as np
from datetime import timedelta, date
import io

st.set_page_config(layout="wide")
st.title("Material Requirement Planning Automation")

# -------------------------------
# 1️⃣ Upload Input Files
# -------------------------------
st.header("Upload Input Files")

col1, col2, col3, col4 = st.columns(4)
with col1:
    inventory_file = st.file_uploader("Inventory On Hand", type=["csv", "xlsx"])
with col2:
    issuance_file = st.file_uploader("Historical Weekly Issuance", type=["csv", "xlsx"])
with col3:
    receipts_file = st.file_uploader("Scheduled Receipts", type=["csv", "xlsx"])
with col4:
    item_master_file = st.file_uploader("Item Master", type=["csv", "xlsx"])

if inventory_file and issuance_file and receipts_file and item_master_file:

    # -------------------------------
    # ROBUST FILE LOADER (Cloud Safe)
    # -------------------------------
    def load_file(f):
        if f.name.lower().endswith(".xlsx"):
            return pd.read_excel(f)
        elif f.name.lower().endswith(".csv"):
            try:
                return pd.read_csv(f, encoding="utf-8")
            except UnicodeDecodeError:
                try:
                    return pd.read_csv(f, encoding="utf-8-sig")
                except UnicodeDecodeError:
                    try:
                        return pd.read_csv(f, encoding="latin1")
                    except Exception:
                        st.error(f"Unable to read CSV file: {f.name}")
                        st.stop()
        else:
            st.error(f"Unsupported file type: {f.name}")
            st.stop()

    inventory = load_file(inventory_file)
    issuance = load_file(issuance_file)
    receipts = load_file(receipts_file)
    items = load_file(item_master_file)

    # Ensure numeric columns are numeric
    for col in ["on_hand_qty"]:
        if col in inventory.columns:
            inventory[col] = pd.to_numeric(inventory[col], errors="coerce").fillna(0)
    for col in ["issued_qty"]:
        if col in issuance.columns:
            issuance[col] = pd.to_numeric(issuance[col], errors="coerce").fillna(0)
    for col in ["qty"]:
        if col in receipts.columns:
            receipts[col] = pd.to_numeric(receipts[col], errors="coerce").fillna(0)
    for col in ["safety_stock","lead_time","MOQ","pack_size"]:
        if col in items.columns:
            items[col] = pd.to_numeric(items[col], errors="coerce").fillna(0)

    # Ensure UOM is string
    for df in [inventory, issuance, receipts, items]:
        if "uom" in df.columns:
            df["uom"] = df["uom"].astype(str)

    st.success("Files loaded successfully!")

    # -------------------------------
    # 2️⃣ UOM Consistency Check (Table)
    # -------------------------------
    uom_mismatch_rows = []

    for item in items["item_id"].unique():
        uoms = {}
        if "uom" in items.columns:
            uoms["Item Master"] = set([str(x).lower() for x in items.loc[items["item_id"]==item, "uom"].unique()])
        if "uom" in inventory.columns:
            uoms["Inventory"] = set([str(x).lower() for x in inventory.loc[inventory["item_id"]==item, "uom"].unique()])
        if "uom" in issuance.columns:
            uoms["Issuance"] = set([str(x).lower() for x in issuance.loc[issuance["item_id"]==item, "uom"].unique()])
        if "uom" in receipts.columns:
            uoms["Receipts"] = set([str(x).lower() for x in receipts.loc[receipts["item_id"]==item, "uom"].unique()])

        all_uoms = set()
        for u in uoms.values():
            all_uoms.update(u)
        if len(all_uoms) > 1:
            row = {"Item": item}
            for src,u in uoms.items():
                row[src] = ", ".join(u)
            uom_mismatch_rows.append(row)

    if uom_mismatch_rows:
        st.subheader("⚠ UOM Mismatches Detected")
        st.dataframe(pd.DataFrame(uom_mismatch_rows))

    # Convert date columns
    issuance["week_start"] = pd.to_datetime(issuance["week_start"])
    receipts["week_start"] = pd.to_datetime(receipts["week_start"])
    warehouses = inventory["warehouse"].unique()

    # -------------------------------
    # 3️⃣ Warehouse-Specific Item Master
    # -------------------------------
    required_cols = ["warehouse","item_id","description","safety_stock","lead_time","MOQ","pack_size","uom"]
    missing = [c for c in required_cols if c not in items.columns]
    if missing:
        st.error(f"Missing required columns in Item Master: {missing}")
        st.stop()

    dupes = items[items.duplicated(subset=["warehouse","item_id"], keep=False)]
    if not dupes.empty:
        st.error("Duplicate planning parameters found for warehouse-item combination:")
        st.dataframe(dupes)
        st.stop()

    items_dict = items.set_index(["warehouse","item_id"]).to_dict("index")

    # -------------------------------
    # 4️⃣ Future Planning Weeks (Monday Start)
    # -------------------------------
    today = pd.to_datetime(date.today())
    today_monday = today - pd.Timedelta(days=today.weekday())
    num_weeks = 12
    time_buckets = [today_monday + timedelta(weeks=w) for w in range(num_weeks)]

    # -------------------------------
    # 5️⃣ Initialize MRP Structures
    # -------------------------------
    debug_rows = []

    for wh in warehouses:
        wh_inventory = inventory[inventory["warehouse"]==wh].set_index("item_id")["on_hand_qty"].to_dict()
        wh_items = [item for (w,item) in items_dict.keys() if w==wh]

        for item in wh_items:
            previous_s = wh_inventory.get(item,0)
            params = items_dict.get((wh,item),{})

            safety_stock = params.get("safety_stock",0)
            lead_time = int(params.get("lead_time",1))
            MOQ = int(params.get("MOQ",1))
            pack_size = int(params.get("pack_size",1))
            uom = params.get("uom","")
            description = params.get("description","")

            # Projected demand: average of last 4 weeks
            last4 = issuance[(issuance["warehouse"]==wh) & (issuance["item_id"]==item)].sort_values("week_start").tail(4)
            avg_demand = last4["issued_qty"].mean() if not last4.empty else 0
            avg_demand = max(avg_demand,1)

            for i, bucket in enumerate(time_buckets):
                wkly_req = avg_demand
                incoming = receipts[(receipts["warehouse"]==wh) & (receipts["item_id"]==item) & (receipts["week_start"]==bucket)]
                incoming_qty = incoming["qty"].sum() if not incoming.empty else 0

                end_s = previous_s - wkly_req + incoming_qty
                shortage = max(safety_stock - end_s,0)
                planned_qty = 0
                if shortage > 0:
                    planned_qty = max(shortage,MOQ)
                    planned_qty = int(np.ceil(planned_qty/pack_size)*pack_size)
                    end_s += planned_qty

                debug_rows.append({
                    "warehouse": wh,
                    "item": item,
                    "description": description,
                    "uom": uom,
                    "week": bucket,
                    "Beg_SOH": previous_s,
                    "Wkly_Req": wkly_req,
                    "Incoming": incoming_qty,
                    "Shortage": shortage,
                    "Planned_Order": planned_qty,
                    "End_SOH": end_s,
                    "Safety_Stock": safety_stock
                })

                previous_s = end_s

    debug_df = pd.DataFrame(debug_rows)

    # -------------------------------
    # 6️⃣ Create Parameter-vs-Week Table (with DTL)
    # -------------------------------
    parameters = ["Beg_SOH","Wkly_Req","Incoming","Planned_Order","End_SOH","DTL"]
    long_rows = []
    week_labels = [w.strftime("%b %d, %Y") for w in time_buckets]

    for wh in warehouses:
        wh_debug = debug_df[debug_df["warehouse"]==wh]
        wh_items = wh_debug["item"].unique()
        for item in wh_items:
            item_debug = wh_debug[wh_debug["item"]==item]
            uom = items_dict.get((wh,item),{}).get("uom","")
            description = items_dict.get((wh,item),{}).get("description","")
            for param in parameters:
                row = {"warehouse": wh, "item": item, "description": description, "uom": uom, "parameter": param}
                for i, w in enumerate(time_buckets):
                    val = item_debug[item_debug["week"]==w][param.replace("DTL","End_SOH" if param=="DTL" else param)].values
                    if len(val) > 0:
                        val_scalar = val[0]
                        if param=="DTL":
                            avg_req = item_debug["Wkly_Req"].mean()
                            val_scalar = (val_scalar / avg_req * 7) if avg_req > 0 else 0
                        row[week_labels[i]] = round(val_scalar,2)
                    else:
                        row[week_labels[i]] = 0
                long_rows.append(row)

    param_table = pd.DataFrame(long_rows)

    # -------------------------------
    # 7️⃣ Styling Table
    # -------------------------------
    def style_param_table_by_item(row):
        styles = []
        item_colors = {}
        unique_items = param_table['item'].unique()
        colors = ['#e8e8e8', '#f5f5f5']
        for idx, itm in enumerate(unique_items):
            item_colors[itm] = colors[idx % 2]
        base_color = item_colors.get(row['item'], '#ffffff')
        for col in row.index:
            try:
                val = float(row[col])
            except:
                val = 0
            if row['parameter']=='DTL' and val<7:
                styles.append('background-color: #f8d7da; color: black')
            elif row['parameter']=='Planned_Order' and val>0:
                styles.append('background-color: #d4edda; color: black')
            else:
                styles.append(f'background-color: {base_color}; color: black')
        return styles

    st.subheader("MRP Logic Table and Planning Horizon")
    st.dataframe(param_table.style.apply(style_param_table_by_item, axis=1))

    # -------------------------------
    # 8️⃣ Planned Orders Table (Downloadable)
    # -------------------------------
    planned_rows = []
    for _, r in debug_df.iterrows():
        if r["Planned_Order"] > 0:
            params = items_dict.get((r["warehouse"],r["item"]),{})
            lead_time = int(params.get("lead_time",1))
            receipt_week = r["week"]
            release_week = receipt_week - pd.Timedelta(weeks=lead_time)
            planned_rows.append({
                "warehouse": r["warehouse"],
                "item": r["item"],
                "description": params.get("description",""),
                "uom": r["uom"],
                "planned_qty": r["Planned_Order"],
                "receipt_week": receipt_week.strftime("%b %d, %Y"),
                "release_week": release_week.strftime("%b %d, %Y")
            })

    planned_df = pd.DataFrame(planned_rows)
    st.subheader("Upcoming Planned Orders")
    if planned_df.empty:
        st.warning("No upcoming planned orders.")
    else:
        planned_df["release_week_dt"] = pd.to_datetime(planned_df["release_week"], format="%b %d, %Y")
        planned_df.sort_values(["release_week_dt","warehouse","item"], inplace=True)
        planned_df.drop(columns=["release_week_dt"], inplace=True)
        st.dataframe(planned_df)

        # Excel download
        output = io.BytesIO()
        planned_df.to_excel(output, index=False, engine='openpyxl')
        output.seek(0)
        st.download_button(
            label="📥 Download Planned Orders as Excel",
            data=output,
            file_name="planned_orders.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
