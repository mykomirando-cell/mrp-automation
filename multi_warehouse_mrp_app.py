import streamlit as st
import pandas as pd
import numpy as np
from datetime import timedelta, date
import io

st.set_page_config(layout="wide")
st.title("Material Requirement Planning Automation")

# -------------------------------
# Helper: Clean Columns
# -------------------------------
def clean_columns(df):
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(" ", "_")
    )
    return df

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

    def load_file(f):
        if f.name.lower().endswith(".xlsx"):
            return pd.read_excel(f)
        else:
            try:
                return pd.read_csv(f, encoding="utf-8")
            except:
                return pd.read_csv(f, encoding="latin1")

    inventory = clean_columns(load_file(inventory_file))
    issuance = clean_columns(load_file(issuance_file))
    receipts = clean_columns(load_file(receipts_file))
    items = clean_columns(load_file(item_master_file))

    # -------------------------------
    # Numeric Cleanup
    # -------------------------------
    for col in ["on_hand_qty"]:
        if col in inventory.columns:
            inventory[col] = pd.to_numeric(inventory[col], errors="coerce").fillna(0)

    for col in ["safety_stock", "lead_time", "moq", "pack_size"]:
        if col in items.columns:
            items[col] = pd.to_numeric(items[col], errors="coerce").fillna(0)

    for col in ["issued_qty"]:
        if col in issuance.columns:
            issuance[col] = pd.to_numeric(issuance[col], errors="coerce").fillna(0)

    for col in ["qty"]:
        if col in receipts.columns:
            receipts[col] = pd.to_numeric(receipts[col], errors="coerce").fillna(0)

    for df in [inventory, issuance, receipts, items]:
        if "uom" in df.columns:
            df["uom"] = df["uom"].astype(str)

    st.success("Files loaded successfully!")

    # -------------------------------
    # Validate Item Master Columns
    # -------------------------------
    required_cols = [
        "warehouse","item_id","description",
        "safety_stock","lead_time","moq","pack_size","uom"
    ]

    missing = [c for c in required_cols if c not in items.columns]
    if missing:
        st.error(f"Missing required columns in Item Master: {missing}")
        st.stop()

    dupes = items[items.duplicated(subset=["warehouse","item_id"], keep=False)]
    if not dupes.empty:
        st.error("Duplicate warehouse-item combinations found.")
        st.dataframe(dupes)
        st.stop()

    items_dict = items.set_index(["warehouse","item_id"]).to_dict("index")

    # -------------------------------
    # Date Conversion
    # -------------------------------
    if "week_start" in issuance.columns:
        issuance["week_start"] = pd.to_datetime(issuance["week_start"])
    if "week_start" in receipts.columns:
        receipts["week_start"] = pd.to_datetime(receipts["week_start"])

    # -------------------------------
    # Planning Horizon
    # -------------------------------
    today = pd.to_datetime(date.today())
    today_monday = today - pd.Timedelta(days=today.weekday())
    num_weeks = 12
    time_buckets = [today_monday + timedelta(weeks=w) for w in range(num_weeks)]

    # -------------------------------
    # Pre-group Data (Performance Boost)
    # -------------------------------
    issuance_sorted = issuance.sort_values("week_start")

    issuance_grouped = (
        issuance_sorted
        .groupby(["warehouse","item_id"])["issued_qty"]
        .apply(list)
        .to_dict()
    )

    receipts_grouped = (
        receipts
        .groupby(["warehouse","item_id","week_start"])["qty"]
        .sum()
        .to_dict()
    )

    inventory_grouped = (
        inventory
        .groupby(["warehouse","item_id"])["on_hand_qty"]
        .sum()
        .to_dict()
    )

    # -------------------------------
    # Optimized MRP Engine
    # -------------------------------
    debug_rows = []

    for (wh, item), params in items_dict.items():

        previous_s = float(inventory_grouped.get((wh,item), 0))
        safety_stock = float(params.get("safety_stock", 0))
        lead_time = int(params.get("lead_time", 1))
        moq = int(params.get("moq", 1))
        pack_size = int(params.get("pack_size", 1))
        description = params.get("description", "")
        uom = params.get("uom", "")

        issued_list = issuance_grouped.get((wh, item), [])
        avg_demand = np.mean(issued_list[-4:]) if len(issued_list) > 0 else 0

        if pd.isna(avg_demand) or avg_demand <= 0:
            avg_demand = 1

        avg_demand = float(avg_demand)

        for bucket in time_buckets:

            incoming_qty = float(receipts_grouped.get((wh,item,bucket), 0))

            wkly_req = avg_demand
            end_s = previous_s - wkly_req + incoming_qty
            shortage = max(safety_stock - end_s, 0)

            planned_qty = 0

            if shortage > 0:
                planned_qty = max(shortage, moq)
                if pack_size > 0:
                    planned_qty = np.ceil(planned_qty / pack_size) * pack_size
                planned_qty = float(planned_qty)
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
                "Planned_Order": planned_qty,
                "End_SOH": end_s,
            })

            previous_s = end_s

    debug_df = pd.DataFrame(debug_rows)

    # -------------------------------
    # Parameter Table
    # -------------------------------
    parameters = ["Beg_SOH","Wkly_Req","Incoming","Planned_Order","End_SOH"]
    long_rows = []
    week_labels = [w.strftime("%b %d, %Y") for w in time_buckets]

    for (wh, item), _ in items_dict.items():
        item_debug = debug_df[
            (debug_df["warehouse"]==wh) &
            (debug_df["item"]==item)
        ]

        for param in parameters:
            row = {
                "warehouse": wh,
                "item": item,
                "description": item_debug["description"].iloc[0],
                "uom": item_debug["uom"].iloc[0],
                "parameter": param
            }

            for i, w in enumerate(time_buckets):
                val = item_debug[item_debug["week"]==w][param].values
                row[week_labels[i]] = float(val[0]) if len(val)>0 else 0

            long_rows.append(row)

    param_table = pd.DataFrame(long_rows)

    numeric_cols = param_table.select_dtypes(include=["float","int"]).columns
    param_table[numeric_cols] = param_table[numeric_cols].astype(float).round(2)

    st.subheader("MRP Logic Table and Planning Horizon")
    st.dataframe(param_table.style.format("{:.2f}"))

    # -------------------------------
    # Planned Orders
    # -------------------------------
    planned_df = debug_df[debug_df["Planned_Order"] > 0].copy()

    if planned_df.empty:
        st.warning("No upcoming planned orders.")
    else:
        planned_df.rename(columns={"Planned_Order":"planned_qty"}, inplace=True)
        st.subheader("Upcoming Planned Orders")
        st.dataframe(planned_df.style.format({"planned_qty":"{:.2f}"}))

        output = io.BytesIO()
        planned_df.to_excel(output, index=False, engine="openpyxl")
        output.seek(0)

        st.download_button(
            "📥 Download Planned Orders as Excel",
            data=output,
            file_name="planned_orders.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
