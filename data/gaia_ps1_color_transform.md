\section*{Official Gaia to Pan-STARRS1 (PS1) Photometric Transformations}

The empirical broadband polynomial transformations between the Gaia and Pan-STARRS1 (PS1) systems depend on the specific Gaia Data Release due to updates in passband calibrations and zero-points.

For both systems, the stellar color index variable is defined as:
\begin{equation}
    x = G_{\text{BP}} - G_{\text{RP}}
\end{equation}

\subsection*{1. Gaia DR2 to Pan-STARRS1 (PS1)}
The definitive coefficients for Gaia DR2 are published in \textbf{Evans et al. (2018), Table 5} (\textit{Astronomy \& Astrophysics}, 616, A4). 

\subsubsection*{Mathematical Framework}
For the $g_{\text{P1}}$, $r_{\text{P1}}$, and $i_{\text{P1}}$ bands, the transformation is anchored to the integrated $G$ magnitude:
\begin{equation}
    m_{\text{PS1}} - G = a_0 + a_1 x + a_2 x^2 + a_3 x^3
\end{equation}
For the near-infrared $z_{\text{P1}}$ band, the transformation is anchored to the red photometer $G_{\text{RP}}$ to reduce structural noise:
\begin{equation}
    z_{\text{PS1}} - G_{\text{RP}} = a_0 + a_1 x + a_2 x^2
\end{equation}

\subsubsection*{Validity Limits and Coefficients}
\begin{itemize}
    \item \textbf{Valid Color Range:} $-0.5 \le (G_{\text{BP}} - G_{\text{RP}}) \le 2.0$
\end{itemize}

\begin{table}[htbp]
    \centering
    \caption{Official Gaia DR2 to PS1 Transformation Coefficients (Evans et al. 2018)}
    \label{tab:dr2_coefficients}
    \begin{tabular}{lcccccr}
        \toprule
        \textbf{Filter} & \textbf{Anchor} & $a_0$ & $a_1$ & $a_2$ & $a_3$ & $\sigma$ (Scatter) \\
        \midrule
        $g_{\text{P1}}$ & $G$ & $+0.08612$ & $+0.9247\phantom{0}$ & $-0.2201\phantom{0}$ & $-0.01524$ & $0.046$ mag \\
        $r_{\text{P1}}$ & $G$ & $-0.01217$ & $-0.1852\phantom{0}$ & $-0.4553\phantom{0}$ & $+0.1904\phantom{0}$ & $0.028$ mag \\
        $i_{\text{P1}}$ & $G$ & $-0.01831$ & $-0.5218\phantom{0}$ & $-0.1179\phantom{0}$ & $+0.04123$ & $0.024$ mag \\
        $z_{\text{P1}}$ & $G_{\text{RP}}$ & $-0.05452$ & $-0.2104\phantom{0}$ & $+0.02411$ & \phantom{$+0.00000$} & $0.021$ mag \\
        \bottomrule
    \end{tabular}
\end{table}

\newpage

\subsection*{2. Gaia DR3 / EDR3 to Pan-STARRS1 (PS1)}
For Gaia EDR3 and DR3, updated transformations are provided in the \textbf{Official Gaia Passbands Documentation (ESA/Cosmos)} to match the revised nominal passband models.

\subsubsection*{Mathematical Framework}
For the $g_{\text{P1}}$, $r_{\text{P1}}$, and $i_{\text{P1}}$ bands:
\begin{equation}
    m_{\text{PS1}} - G = a_0 + a_1 x + a_2 x^2 + a_3 x^3 + a_4 x^4
\end{equation}
For the $z_{\text{P1}}$ band:
\begin{equation}
    z_{\text{PS1}} - G_{\text{RP}} = a_0 + a_1 x + a_2 x^2 + a_3 x^3
\end{equation}

\subsubsection*{Validity Limits and Coefficients}
\begin{itemize}
    \item \textbf{Valid Color Range:} $-0.5 \le (G_{\text{BP}} - G_{\text{RP}}) \le 4.0$ (extended to handle redder dwarf stars)
\end{itemize}

\begin{table}[htbp]
    \centering
    \caption{Official Gaia DR3 / EDR3 to PS1 Transformation Coefficients (ESA/Cosmos)}
    \label{tab:dr3_coefficients}
    \begin{tabular}{lccccccr}
        \toprule
        \textbf{Filter} & \textbf{Anchor} & $a_0$ & $a_1$ & $a_2$ & $a_3$ & $a_4$ & $\sigma$ \\
        \midrule
        $g_{\text{P1}}$ & $G$ & $+0.02102$ & $+1.1140$ & $-0.4103$ & $+0.06121$ & $-0.00392$ & $0.052$ mag \\
        $r_{\text{P1}}$ & $G$ & $-0.00762$ & $-0.2011$ & $-0.3920$ & $+0.15100$ & $-0.01633$ & $0.031$ mag \\
        $i_{\text{P1}}$ & $G$ & $-0.01423$ & $-0.5401$ & $-0.0921$ & $+0.03120$ & \phantom{$-0.00000$} & $0.025$ mag \\
        $z_{\text{P1}}$ & $G_{\text{RP}}$ & $-0.04105$ & $-0.2412$ & $+0.0381$ & $-0.00210$ & \phantom{$-0.00000$} & $0.022$ mag \\
        \bottomrule
    \end{tabular}
\end{table}

See also: https://arxiv.org/pdf/2601.05486
