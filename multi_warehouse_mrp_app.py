import streamlit as st
import pandas as pd
import numpy as np
from datetime import timedelta, date
import io
from pandas.errors import EmptyDataError

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

# -------------------------------
# ROBUST FILE LOADER
# -------------------------------
def load_file(f):
    if not f:
        st.error("No file provided.")
        st.stop()

    if f.name.lower().endswith(".xlsx"):
        try:
            return pd.read_excel(f)
        except Exception as e:
            st.error(f"Unable to read Excel file {f.name}: {e}")
            st.stop()

    elif f.name.lower().endswith(".csv"):
        for enc in ["utf-8", "utf-8-sig", "latin1"]:
            try:
                return pd.read_csv(f, encoding=enc)
            except UnicodeDecodeError:
                continue
            except EmptyDataError:
                st.error(f"The CSV file {f.name} is empty or malformed.")
                st.stop()
            except Exception as e:
                st.error(f"Error reading CSV file {f.name} with encoding {enc}: {e}")
                st.stop()
        st.error(f"Unable to read CSV file {f.name} with utf-8/utf-8-sig/latin1 encodings.")
        st.stop()

    else:
        st.error(f"Unsupported file type: {f.name}")
        st.stop()

if inventory_file and issuance_file and receipts_file and item_master_file:
    # Load files
    inventory = load_file(inventory_file)
    issuance = load_file(issuance_file)
    receipts = load_file(receipts_file)
    items = load_file(item_master_file)

    # Ensure UOM is string and lowercase (for case-insensitive comparison)
    for df in [inventory, issuance, receipts, items]:
        if "uom" in df.columns:
            df["uom"] = df["uom"].astype(str).str.lower()

    st.success("Files loaded successfully!")

    # -------------------------------
    # 2️⃣ UOM Consistency Check (non-blocking)
    # -------------------------------
    uom_warnings = []
    for item in items["item_id"].unique():
        uoms = {}
        uoms["item_master"] = set(items.loc[items["item_id"]==item, "uom"].str.lower())
        uoms["inventory"] = set(inventory.loc[inventory["item_id"]==item, "uom"].str.lower())
        uoms["issuance"] = set(issuance.loc[issuance["item_id"]==item, "uom"].str.lower())
        uoms["receipts"] = set(receipts.loc[receipts["item_id"]==item, "uom"].str.lower())
        all_uoms = set.union(*uoms.values())
        if len(all_uoms) > 1:
            uom_warnings.append({"item_id": item, "uoms": uoms})
    if uom_warnings:
        st.warning("⚠ UOM mismatches detected (case-insensitive comparison):")
        st.json(uom_warnings)

    # -------------------------------
    # 3️⃣ Convert date columns
    # -------------------------------
    if "week_start" in issuance.columns:
        issuance["week_start"] = pd.to_datetime(issuance["week_start"])
    if "week_start" in receipts.columns:
        receipts["week_start"] = pd.to_datetime(receipts["week_start"])
    warehouses = inventory["warehouse"].unique()

    # -------------------------------
    # 4️⃣ Warehouse-Specific Item Master Checks
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
    # 5️⃣ Future Planning Weeks (Monday Start)
    # -------------------------------
    today = pd.to_datetime(date.today())
    today_monday = today - pd.Timedelta(days=today.weekday())
    num_weeks = 12
    time_buckets = [today_monday + timedelta(weeks=w) for w in range(num_weeks)]

    # -------------------------------
    # 6️⃣ Initialize MRP Structures
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
    # 7️⃣ Planned Orders Table
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
