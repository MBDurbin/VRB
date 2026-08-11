import math
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os

# --- Geometry & System Constants ---
# Kept global so helper functions can access them without needing them passed every time
D = 0.060
ST = 0.124
SL = 0.152
SD = 0.165
NL = 3
duct_area = 0.61 * 0.61  # 2ft x 2ft in m^2
A_cylinder = 0.094


def get_gas_properties(temp_k):
    """
    Interpolates gas properties from a CSV file for a given temperature.
    """
    file_path = r"C:\Users\Durbi\PycharmProjects\Resistor Bank Master\Gas_Properties_ATMP.csv"

    try:
        df = pd.read_csv(file_path)
        df.columns = df.columns.str.strip()
        t_values = df['T (K)'].values

        if temp_k < t_values.min() or temp_k > t_values.max():
            return {
                "Error": f"Temperature {temp_k}K is outside the data range ({t_values.min()}K - {t_values.max()}K)."}

        interpolated_data = {}
        for column in df.columns:
            if column == 'T (K)':
                interpolated_data[column] = float(temp_k)
            else:
                val = np.interp(temp_k, df['T (K)'], df[column])
                interpolated_data[column] = round(val, 6)

        return interpolated_data

    except FileNotFoundError:
        return {"Error": "The CSV file was not found at the specified path."}
    except Exception as e:
        return {"Error": str(e)}


# --- Helper Functions ---
def calc_V_max(V):
    if 2 * (SD - D) < (ST - D):
        V_max = (ST / (2 * (SD - D))) * V
    else:
        V_max = (ST / (ST - D)) * V
    return V_max


def calc_Re_D(V_max, nu):
    return V_max * D / nu


def CFM_to_V(CFM):
    volumetric_flow_m3s = CFM * 0.000471947
    return volumetric_flow_m3s / duct_area


def CFM_to_m_dot(CFM, rho):
    volumetric_flow_m3s = CFM * 0.000471947
    return rho * volumetric_flow_m3s


def calc_Nu_D(Re_D, Pr, Pr_s):
    if Re_D < (2 * (10 ** 5)):
        m = 0.6
        if ST / SL < 2:
            C1 = 0.35 * ((ST / SL) ** 0.2)
        elif ST / SL > 2:
            C1 = 0.4
        else:
            C1 = 0.4  # Fallback if exactly 2
    else:
        m = 0.84
        C1 = 0.022

    # Map NL to C2
    nl_mapping = {
        1: 0.64, 2: 0.76, 3: 0.84, 4: 0.89, 5: 0.92,
        6: (0.95 + 0.92) / 2, 7: 0.95
    }

    if NL in nl_mapping:
        C2 = nl_mapping[NL]
    elif 7 < NL < 10:
        C2 = 0.96
    elif 10 <= NL < 13:
        C2 = 0.97
    elif 13 <= NL < 16:
        C2 = 0.98
    elif 16 <= NL < 20:
        C2 = 0.99
    else:
        C2 = 1

    Nu_D = C2 * C1 * (Re_D ** m) * (Pr ** 0.36) * ((Pr / Pr_s) ** 0.25)
    return Nu_D


# --- Main Function ---
def find_CFM(T_s_K, active_resistors, target_power_W=8000, T_in_K=300, start_cfm=2000, cfm_step=1):
    """
    Iterates to find the minimum CFM required to dissipate the target power.

    Args:
        T_s_K (float): Surface temperature of the resistors in Kelvin.
        active_resistors (int): Number of active resistors in the bank.
        target_power_W (float): Target power to dissipate (Default: 8000 W).
        T_in_K (float): Inlet air temperature in Kelvin (Default: 300 K).
        start_cfm (int): Initial CFM guess (Default: 2000).
        cfm_step (int): Iteration resolution (Default: 1).

    Returns:
        dict: A dictionary containing the final calculated values.
    """
    A_s_total = active_resistors * A_cylinder
    CFM = start_cfm
    q_calc = 0
    T_final = 400  # Initial guess for the average temperature calculation

    print(f"Iterating to find minimum CFM for {active_resistors} resistors at {T_s_K}K surface temp...")

    while q_calc < target_power_W:
        T_bar = (T_final + T_in_K) / 2
        T_bar_props = get_gas_properties(T_bar)

        if "Error" in T_bar_props:
            raise ValueError(f"Gas property error at T_bar={T_bar}K: {T_bar_props['Error']}")

        Pr = T_bar_props['Pr']
        nu = T_bar_props['nu*10^6 (m2/s)'] * 1e-6
        rho = T_bar_props['rho (kg/m3)']
        cp = T_bar_props['Cp (kJ/kg K)'] * 1000
        k = T_bar_props['k*10^3 (W/m K)'] * 1e-3

        V = CFM_to_V(CFM)
        V_max = calc_V_max(V)
        Re_D = calc_Re_D(V_max, nu)

        T_s_props = get_gas_properties(T_s_K)
        if "Error" in T_s_props:
            raise ValueError(f"Gas property error at T_s_K={T_s_K}K: {T_s_props['Error']}")

        Pr_s = T_s_props['Pr']

        Nu_D = calc_Nu_D(Re_D, Pr, Pr_s)
        m_dot = CFM_to_m_dot(CFM, rho)

        # Energy Balance
        T_final = T_in_K + (target_power_W / (m_dot * cp))

        # If the air gets hotter than the surface, this CFM is physically impossible
        if T_final >= T_s_K:
            CFM += cfm_step
            continue

        # Calculate Log Mean Temperature Difference (LMTD)
        delta_T1 = T_s_K - T_in_K
        delta_T2 = T_s_K - T_final

        if delta_T1 == delta_T2:
            LMTD = delta_T1
        else:
            LMTD = (delta_T1 - delta_T2) / math.log(delta_T1 / delta_T2)

        # Convection Coefficient (h)
        h = (Nu_D * k) / D

        # Calculate heat dissipated
        q_calc = h * A_s_total * LMTD

        # Iterate
        if q_calc < target_power_W:
            CFM += cfm_step

    # Return the results cleanly
    results = {
        "CFM": CFM,
        "Dissipated_Power_W": round(q_calc, 2),
        "Air_Exit_Temp_K": round(T_final, 2),
        "Convection_Coeff": round(h, 2),
        "Max_Reynolds": round(Re_D, 0),
        "Air_Velocity_m_s": round(V, 2)
    }
    print(T_bar_props)

    return results


def plot_cfm_effects():
    # --- 1. Effect of Number of Resistors ---
    # Keeping Surface Temp constant at 450K
    constant_temp = 450
    resistor_range = list(range(4, 21))  # 4 to 20
    cfm_by_resistors = []

    print("Calculating effect of resistor count...")
    for r in resistor_range:
        try:
            res = find_CFM(T_s_K=constant_temp, active_resistors=r, start_cfm=500, cfm_step=5)
            cfm_by_resistors.append(res['CFM'])
        except Exception as e:
            print(f"Skipping {r} resistors due to error: {e}")
            cfm_by_resistors.append(None)

    # --- 2. Effect of Surface Temperature ---
    # Keeping Active Resistors constant at 11
    constant_resistors = 11
    temp_range = list(range(350, 651, 10))  # 350K to 650K in steps of 10
    cfm_by_temp = []

    print("\nCalculating effect of surface temperature...")
    for t in temp_range:
        try:
            res = find_CFM(T_s_K=t, active_resistors=constant_resistors, start_cfm=500, cfm_step=5)
            cfm_by_temp.append(res['CFM'])
        except Exception as e:
            print(f"Skipping {t}K due to error: {e}")
            cfm_by_temp.append(None)

    # --- 3. Plotting and Saving the Results ---
    print("\nGenerating and saving plots...")

    # Plot 1: Resistors vs CFM
    plt.figure(figsize=(8, 6))
    plt.plot(resistor_range, cfm_by_resistors, marker='o', color='b', linestyle='-', linewidth=2)
    plt.xlabel('Number of Active Resistors', fontsize=12)
    plt.ylabel('Minimum Required CFM', fontsize=12)
    plt.xticks(range(4, 21, 2))
    plt.grid(False)
    plt.tight_layout()

    # Save the first figure
    resistor_filename = 'CFM_vs_Resistor_Count.png'
    plt.savefig(resistor_filename, dpi=300)
    plt.close()  # Close the figure to free up memory
    print(f"Saved: {os.path.abspath(resistor_filename)}")

    # Plot 2: Temperature vs CFM
    plt.figure(figsize=(8, 6))
    plt.plot(temp_range, cfm_by_temp, marker='s', color='r', linestyle='-', linewidth=2, markersize=5)
    plt.xlabel('Target Surface Temperature (K)', fontsize=12)
    plt.ylabel('Minimum Required CFM', fontsize=12)
    plt.grid(False)
    plt.tight_layout()

    # Save the second figure
    temp_filename = 'CFM_vs_Surface_Temp.png'
    plt.savefig(temp_filename, dpi=300)
    plt.close()  # Close the figure to free up memory
    print(f"Saved: {os.path.abspath(temp_filename)}")


if __name__ == "__main__":
    # plot_cfm_effects()
    results = find_CFM(450, 11, start_cfm=2600, cfm_step=1)
    print(results)