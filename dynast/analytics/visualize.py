# Copyright (c) 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import alphashape
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tqdm
from descartes import PolygonPatch
from matplotlib.cm import ScalarMappable
from matplotlib.ticker import ScalarFormatter
from pymoo.indicators.hv import HV
from scipy.spatial import Delaunay
from shapely.geometry import MultiLineString, MultiPoint, mapping
from shapely.ops import cascaded_union, polygonize
from sklearn.preprocessing import MinMaxScaler

from dynast.utils import log


@dataclass
class ReferencePoint(object):
    label: str
    metrics: Dict[str, float]
    marker: str = '*'
    color: str = 'red'


PLOT_HYPERVOLUME = True
NORMALIZE = False

colors = {
    'blue': '#1f77b4',
    'orange': '#ff7f0e',
    'green': '#2ca02c',
    'red': '#d62728',
    'purple': '#9467bd',
    'brown': '#8c564b',
    'pink': '#e377c2',
    'gray': '#7f7f7f',
    'olive': '#bcbd22',
    'cyan': '#17becf',
}


def sanitize_label(label: str, normalize: bool = False):
    if label == 'latency' and normalize:
        label = f'Latency (normalized)'
    elif label == 'latency':
        label = 'Latency (ms)'
    elif label == 'accuracy_sst2':
        label = 'Accuracy (%)'
    elif label == 'accuracy_top1':
        label = 'Top-1 Accuracy (%)'
    elif label == 'model_size':
        label = 'Model Size (MB)'
    return label


def frontier_builder(df, optimization_metrics, alpha=0):
    """
    Modified alphashape algorithm to draw Pareto Front for OFA search.
    Takes a DataFrame of column form [x, y] = [latency, accuracy]

    Params:
    df     - dataframe containing `optimization_metrics` columns at minimum
    alpha  - Dictates amount of tolerable 'concave-ness' allowed.
             A fully convex front will be given if 0 (also better for runtime)
    """
    log.debug('Running front builder')
    df = df[optimization_metrics]
    points = list(df.to_records(index=False))
    for i in range(len(points)):
        points[i] = list(points[i])
    points = MultiPoint(points)

    if len(points.geoms) < 4 or alpha <= 0:
        log.debug('Alpha=0 -> convex hull')
        result = points.convex_hull
    else:
        coords = np.array([point.coords[0] for point in points])
        tri = Delaunay(coords)
        edges = set()
        edge_points = []
        edge_out = []

        # Loop over triangles
        for ia, ib, ic in tri.vertices:
            pa = coords[ia]
            pb = coords[ib]
            pc = coords[ic]

            # Lengths of sides of triangle
            a = math.sqrt((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2)
            b = math.sqrt((pb[0] - pc[0]) ** 2 + (pb[1] - pc[1]) ** 2)
            c = math.sqrt((pc[0] - pa[0]) ** 2 + (pc[1] - pa[1]) ** 2)

            # Semiperimeter of triangle
            s = (a + b + c) * 0.5

            # Area of triangle by Heron's formula
            # Precompute value inside square root to avoid unbound math error in
            # case of 0 area triangles.
            area = s * (s - a) * (s - b) * (s - c)

            if area > 0:
                area = math.sqrt(area)

                # Radius Filter
                if a * b * c / (4.0 * area) < 1.0 / alpha:
                    for i, j in itertools.combinations([ia, ib, ic], r=2):
                        if (i, j) not in edges and (j, i) not in edges:
                            edges.add((i, j))
                            edge_points.append(coords[[i, j]])

                            if coords[i].tolist() not in edge_out:
                                edge_out.append(coords[i].tolist())
                            if coords[j].tolist() not in edge_out:
                                edge_out.append(coords[j].tolist())

        # Create the resulting polygon from the edge points
        m = MultiLineString(edge_points)
        triangles = list(polygonize(m))
        result = cascaded_union(triangles)

    # Find multi-polygon boundary
    bound = list(mapping(result.boundary)['coordinates'])

    # Cutoff non-Pareto front points
    # note that extreme concave geometries will create issues if bi-sected by line
    df = pd.DataFrame(bound, columns=['x', 'y'])

    # y=mx+b
    left_point = (df.iloc[df.idxmin()[0]][0], df.iloc[df.idxmin()[0]][1])
    right_point = (df.iloc[df.idxmax()[1]][0], df.iloc[df.idxmax()[1]][1])
    m = (left_point[1] - right_point[1]) / (left_point[0] - right_point[0])
    b = left_point[1] - (m * left_point[0])

    df = df[df['y'] >= (m * df['x'] + b)]
    df.sort_values(by='x', inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Cleanup - insure accuracy is always increasing with latency up the Pareto front
    best_acc = 0
    drop_list = []
    for i in range(len(df)):
        if df.iloc[i]['y'] > best_acc:
            best_acc = df.iloc[i]['y']
        else:
            drop_list.append(i)
    df.drop(df.index[drop_list], inplace=True)
    df.reset_index(drop=True, inplace=True)

    df.columns = optimization_metrics

    return df


def plot_search_progression(
    results_path: str,
    evals_limit: Optional[int] = None,
    random_results_path: Optional[str] = None,
    target_metrics: List[str] = ['latency', 'accuracy_top1'],
    reference_points: List[ReferencePoint] = [],
    columns: List[str] = ['config', 'date', 'params', 'latency', 'macs', 'accuracy_top1'],
    normalize=False,
    title=None,
    xlim: Optional[Tuple[float, float]] = None,
    ylim: Optional[Tuple[float, float]] = None,
) -> None:
    df = pd.read_csv(results_path)
    log.info(f'Loaded {len(df)} entries from {results_path}')
    if evals_limit:
        df = df[:evals_limit]

    df.columns = columns

    if 'accuracy_sst2' in target_metrics:
        df['accuracy_sst2'] = df['accuracy_sst2'] * 100

        for rp in reference_points:
            rp.metrics['accuracy_sst2'] = (
                rp.metrics['accuracy_sst2'] * 100 if rp.metrics['accuracy_sst2'] <= 1.0 else rp.metrics['accuracy_sst2']
            )

    fig, ax = plt.subplots(figsize=(7, 5))

    cm = plt.cm.get_cmap('viridis_r')
    count = [x for x in range(len(df))]

    if random_results_path:
        df_random = pd.read_csv(random_results_path)
        df_random.columns = columns
        log.info(f'Random entries: {len(df_random)}')
        ax.scatter(
            df_random[target_metrics[0]].values,
            df_random[target_metrics[1]].values,
            marker='.',
            alpha=0.1,
            c='grey',
            label='Random DNN Model',
        )
        cloud = list(df_random[[target_metrics[0], target_metrics[1]]].to_records(index=False))
        for i in range(len(cloud)):
            cloud[i] = list(cloud[i])
        print(cloud[:5])
        alpha_shape = alphashape.alphashape(cloud, 0.0)
        print(alpha_shape)
        ax.add_patch(PolygonPatch(alpha_shape, alpha=0.2))
        # ax.add_patch(
        #     PolygonPatch(
        #         alpha_shape,
        #         fill=None,
        #         alpha=0.4,
        #         color='grey',
        #         linewidth=1.5,
        #         label='Random search boundary',
        #         linestyle='--',
        #     )
        # )

    if normalize and 'latency' in target_metrics:
        column = 'latency'
        norm_min = min(
            df[column].min(), min([rp.metrics['latency'] for rp in reference_points] if reference_points else [99999])
        )
        norm_max = max(
            df[column].max(), max([rp.metrics['latency'] for rp in reference_points] if reference_points else [0])
        )
        df[column] = (df[column] - norm_min) / (norm_max - norm_min)

        for i in range(len(reference_points)):
            reference_points[i].metrics[column] = (reference_points[i].metrics[column] - norm_min) / (
                norm_max - norm_min
            )

    ax.scatter(
        df[target_metrics[0]].values,
        df[target_metrics[1]].values,
        marker='^',
        alpha=0.7,
        c=count,
        cmap=cm,
        label='Discovered DNN Model',
        s=10,
    )

    if ylim:
        ax.set_ylim(*ylim)
    if xlim:
        ax.set_xlim(*xlim)

    df_conc_front = frontier_builder(df, optimization_metrics=[target_metrics[0], target_metrics[1]])
    ax.plot(
        df_conc_front[target_metrics[0]],
        df_conc_front[target_metrics[1]],
        color='red',
        linestyle='--',
        label='DyNAS-T Pareto front',
    )

    for reference_point in reference_points:
        ax.scatter(
            reference_point.metrics[target_metrics[0]],
            reference_point.metrics[target_metrics[1]],
            color=reference_point.color,
            marker=reference_point.marker,
            label=reference_point.label,
        )

    # Eval Count bar
    norm = plt.Normalize(0, len(df))
    sm = ScalarMappable(norm=norm, cmap=cm)
    cbar = fig.colorbar(sm, ax=ax, shrink=0.85)
    cbar.ax.set_title("         Evaluation\n  Count", fontsize=8)

    title = 'DyNAS-T Search Results \n{}'.format(results_path.split('.')[0]) if title is None else title
    ax.set_title(title, fontweight="bold")

    x_label = sanitize_label(label=target_metrics[0], normalize=normalize)
    y_label = sanitize_label(label=target_metrics[1], normalize=normalize)

    ax.set_xlabel(x_label, fontsize=13)
    ax.set_ylabel(y_label, fontsize=13)
    ax.legend(
        fancybox=True,
        fontsize=10,
        framealpha=1,
        borderpad=0.2,
        loc='lower right',
    )
    ax.grid(True, alpha=0.3)

    fig.tight_layout(pad=0.3)
    save_path = '{}.png'.format(results_path.split('.')[0])
    plt.savefig(save_path)
    log.info(f'Search progression plot saved to {save_path}')


def load_csv(
    filepath,
    col_list=['config', 'date', 'params', 'latency', 'macs', 'accuracy_top1'],
    normalize=False,
    idx_slicer=None,
    fit=False,
    scaler=None,
    sort=False,
    verbose=False,
):
    # Sub-network,Date,Model Parameters,Latency (ms),MACs,SST-2 Acc
    if idx_slicer is not None:
        df = pd.read_csv(filepath).iloc[:idx_slicer]
    else:
        df = pd.read_csv(filepath)

    df.columns = col_list

    if sort:
        df = df.sort_values(by=['macs']).reset_index(drop=True)
    if verbose:
        print(filepath)
        print('dataset length: {}'.format(len(df)))
        print('acc max = {}'.format(df['accuracy_top1'].max()))
        print('lat min = {}'.format(df['accuracy_top1'].min()))

    df = df[['macs', 'accuracy_top1']]

    if normalize:
        if fit == True:
            scaler = MinMaxScaler()
            scaler.fit(df['macs'].values.reshape(-1, 1))
            df['macs'] = scaler.transform(df['macs'].values.reshape(-1, 1)).squeeze()
            return df, scaler
        else:
            df['macs'] = scaler.transform(df['macs'].values.reshape(-1, 1)).squeeze()
            return df
    else:
        return df


def collect_hv(hv, supernet):
    start_interval = np.array(list(range(10, 200, 10)))
    end_interval = np.array(list(range(200, 10000, 100)))
    full_interval = np.concatenate([start_interval, end_interval])
    hv_list = list()

    for evals in tqdm.tqdm(full_interval):
        front = frontier_builder(supernet.iloc[:evals], optimization_metrics=['macs', 'accuracy_top1'])
        front['naccuracy_top1'] = -front['accuracy_top1']

        hv_list.append(hv.do(front[['macs', 'naccuracy_top1']].values))

    for i in range(0, len(hv_list) - 1):
        if hv_list[i + 1] < hv_list[i]:
            hv_list[i + 1] = hv_list[i]

    full_interval = np.insert(full_interval, 0, 1, axis=0)
    hv_list = np.array(hv_list)
    hv_list = np.insert(hv_list, 0, 0, axis=0)

    return hv_list, full_interval


def plot_hv():
    plot_subtitle = 'BERT SST-2 {step} ({samples}/{evaluations})'
    save_dir = 'output_2/'
    population = 50
    EVALUATIONS = 2000
    xlim = None
    ylim = None  # (73.0, 77.5)
    avg_time = 5.9
    ref_x = [6e9]
    ref_y = [0.90]

    df_linas = load_csv('results/bert_sst2_linas_2000_37.csv', normalize=False, idx_slicer=EVALUATIONS)
    df_nsga = load_csv('results/bert_sst2_nsga2_0.csv', normalize=False, idx_slicer=EVALUATIONS)
    df_random = load_csv('results/bert_sst2_random_0.csv', normalize=False, idx_slicer=EVALUATIONS)
    xlabel = 'MACs'

    df_linas_front = frontier_builder(df_linas, optimization_metrics=['macs', 'accuracy_top1'])
    df_nsga_front = frontier_builder(df_nsga, optimization_metrics=['macs', 'accuracy_top1'])

    evals_list = np.array(list(range(10, 10000, 10)))
    ref_point = [ref_x[0], -ref_y[0]]  # latency, -top1
    hv = HV(ref_point=np.array(ref_point))
    edge_points = []

    ## LINAS
    df_seed1 = load_csv('results/bert_sst2_linas_2000_37.csv', idx_slicer=EVALUATIONS)
    hv_seed1, interval = collect_hv(hv, df_seed1)
    df_seed2 = load_csv('results/bert_sst2_linas_2000_47.csv', idx_slicer=EVALUATIONS)
    hv_seed2, _ = collect_hv(hv, df_seed2)
    df_seed3 = load_csv('results/bert_sst2_linas_2000_57.csv', idx_slicer=EVALUATIONS)
    hv_seed3, _ = collect_hv(hv, df_seed3)

    df_linas_hv = pd.DataFrame(np.vstack((hv_seed1, hv_seed2, hv_seed3)).T)  # Stack all runs from a given search
    df_linas_hv['mean'] = df_linas_hv.mean(axis=1)
    df_linas_hv['std'] = df_linas_hv.std(axis=1) / 3**0.5
    edge_points.append(min(df_linas_hv['mean'][population:] - df_linas_hv['std'][population:]))
    edge_points.append(max(df_linas_hv['mean'][population:] + df_linas_hv['std'][population:]))

    ## NSGA2-2
    df_seed1 = load_csv('results/bert_sst2_nsga2_2000_37.csv', idx_slicer=EVALUATIONS)
    hv_seed1, interval = collect_hv(hv, df_seed1)
    df_seed2 = load_csv('results/bert_sst2_nsga2_2000_47.csv', idx_slicer=EVALUATIONS)
    hv_seed2, _ = collect_hv(hv, df_seed2)
    df_seed3 = load_csv('results/bert_sst2_nsga2_2000_57.csv', idx_slicer=EVALUATIONS)
    hv_seed3, _ = collect_hv(hv, df_seed3)

    df_full_hv = pd.DataFrame(np.vstack((hv_seed1, hv_seed2, hv_seed3)).T)
    df_full_hv['mean'] = df_full_hv.mean(axis=1)
    df_full_hv['std'] = df_full_hv.std(axis=1) / 3**0.5
    edge_points.append(min(df_full_hv['mean'][population:] - df_full_hv['std'][population:]))
    edge_points.append(max(df_full_hv['mean'][population:] + df_full_hv['std'][population:]))

    ## RANDOM
    df_seed1 = load_csv('results/bert_sst2_random_2000_37.csv', idx_slicer=EVALUATIONS)
    hv_seed1, interval = collect_hv(hv, df_seed1)
    df_seed2 = load_csv('results/bert_sst2_random_2000_47.csv', idx_slicer=EVALUATIONS)
    hv_seed2, _ = collect_hv(hv, df_seed2)
    df_seed3 = load_csv('results/bert_sst2_random_2000_57.csv', idx_slicer=EVALUATIONS)
    hv_seed3, _ = collect_hv(hv, df_seed3)

    df_rand_hv = pd.DataFrame(np.vstack((hv_seed1, hv_seed2, hv_seed3)).T)
    df_rand_hv['mean'] = df_rand_hv.mean(axis=1)
    df_rand_hv['std'] = df_rand_hv.std(axis=1) / 3**0.5
    edge_points.append(min(df_rand_hv['mean'][population:] - df_rand_hv['std'][population:]))
    edge_points.append(max(df_rand_hv['mean'][population:] + df_rand_hv['std'][population:]))

    ylim_hv = (min(edge_points) - 0.05 * min(edge_points), max(edge_points) + 0.05 * min(edge_points))

    os.makedirs(save_dir, exist_ok=True)

    for samples in tqdm.tqdm(range(0, EVALUATIONS + 1, population * 4)):
        elapsed_total_m = avg_time * samples
        elapsed_h = int(elapsed_total_m // 60)
        elapsed_m = int(elapsed_total_m - (elapsed_h * 60))
        if PLOT_HYPERVOLUME:
            fig, ax = plt.subplots(1, 3, figsize=(15, 5), gridspec_kw={'width_ratios': [2.5, 3, 2.5]})
        else:
            fig, ax = plt.subplots(1, 2, figsize=(10, 5), gridspec_kw={'width_ratios': [2.5, 3.0]})
        fig.suptitle(
            plot_subtitle.format(
                step=samples // population,
                samples=samples,
                evaluations=EVALUATIONS,
                elapsed_h=elapsed_h,
                elapsed_m=elapsed_m,
            ),
            fontweight="bold",
        )
        cm = plt.cm.get_cmap('viridis_r')

        # LINAS plot
        df_conc = df_linas
        data = df_conc[:samples][['macs', 'accuracy_top1']]
        count = [x for x in range(len(data))]
        x = data['macs']
        y = data['accuracy_top1']

        ax[0].set_title('DyNAS-T')
        ax[0].scatter(x, y, marker='D', alpha=0.8, c=count, cmap=cm, label='Unique DNN\nArchitecture', s=6)
        ax[0].set_ylabel('Accuracy', fontsize=13)
        ax[0].plot(
            df_linas_front['macs'],
            df_linas_front['accuracy_top1'],
            color='red',
            linestyle='--',
            label='DyNAS-T Pareto front',
        )
        # ax[0].scatter(ref_x, ref_y, marker='s', color='#c00', label='Reference ResNset50 OV INT8')

        # NSGA-II plot
        data1 = df_nsga[:samples][['macs', 'accuracy_top1']]
        print(len(data1))
        count = [x for x in range(len(data1))]
        x = data1['macs']
        y = data1['accuracy_top1']

        ax[1].set_title('NSGA-II')
        ax[1].scatter(x, y, marker='D', alpha=0.8, c=count, cmap=cm, label='Unique DNN Architecture', s=6)

        # ax[1].get_yaxis().set_ticklabels([])
        ax[1].plot(
            df_nsga_front['macs'],
            df_nsga_front['accuracy_top1'],
            color='red',
            linestyle='--',
            label='DyNAS-T Pareto front',
        )
        # ax[1].scatter(ref_x, ref_y, marker='s', color='#c00', label='Reference ResNset50 OV INT8')

        cloud = list(df_random[['macs', 'accuracy_top1']].to_records(index=False))
        # alpha_shape = alphashape.alphashape(cloud, 0)

        for ax in fig.get_axes()[:2]:
            # ax.add_patch(PolygonPatch(alpha_shape, fill=None, alpha=0.8, linewidth=1.5, label='Random search boundary', linestyle='--'))

            ax.legend(fancybox=True, fontsize=10, framealpha=1, borderpad=0.2, loc='lower right')
            # if ylim:
            #     ax.set_ylim(ylim)
            # if xlim:
            #     ax.set_xlim(xlim)
            ax.grid(True, alpha=0.3)
            ax.set_xlabel(xlabel, fontsize=13)

        if PLOT_HYPERVOLUME and samples >= population:
            fig.get_axes()[2].set_title('Hypervolume')

            ########## LINAS
            fig.get_axes()[2].plot(interval, df_linas_hv['mean'], label='LINAS', color=colors['red'], linewidth=2)
            fig.get_axes()[2].fill_between(
                interval,
                df_linas_hv['mean'] - df_linas_hv['std'],
                df_linas_hv['mean'] + df_linas_hv['std'],
                color=colors['red'],
                alpha=0.2,
            )
            ########## NSGA / FULL
            fig.get_axes()[2].plot(
                interval, df_full_hv['mean'], label='NSGA-II', linestyle='--', color=colors['blue'], linewidth=2
            )
            fig.get_axes()[2].fill_between(
                interval,
                df_full_hv['mean'] - df_full_hv['std'],
                df_full_hv['mean'] + df_full_hv['std'],
                color=colors['blue'],
                alpha=0.2,
            )
            ########## RANDOM
            fig.get_axes()[2].plot(
                interval, df_rand_hv['mean'], label='Random Search', linestyle='-.', color=colors['orange'], linewidth=2
            )
            fig.get_axes()[2].fill_between(
                interval,
                df_rand_hv['mean'] - df_rand_hv['std'],
                df_rand_hv['mean'] + df_rand_hv['std'],
                color=colors['orange'],
                alpha=0.2,
            )
            ##########

            fig.get_axes()[2].set_xlim(population, samples)
            # if ylim_hv:
            #     fig.get_axes()[2].set_ylim(ylim_hv)

            fig.get_axes()[2].set_xlabel('Evaluation Count', fontsize=13)
            fig.get_axes()[2].set_ylabel('Hypervolume', fontsize=13)
            fig.get_axes()[2].legend(fancybox=True, fontsize=12, framealpha=1, borderpad=0.2, loc='best')

            fig.get_axes()[2].grid(True, alpha=0.2)

            formatter = ScalarFormatter()
            formatter.set_scientific(False)
            fig.get_axes()[2].xaxis.set_major_formatter(formatter)

        # Eval Count bar
        norm = plt.Normalize(0, len(data))
        sm = ScalarMappable(norm=norm, cmap=cm)
        cbar = fig.colorbar(sm, ax=ax, shrink=0.85)
        cbar.ax.set_title("         Evaluation\n  Count", fontsize=8)

        fig.tight_layout(pad=1)
        plt.subplots_adjust(wspace=0.07, hspace=0)
        plt.show()
        fn = save_dir + '/pareto_{}.png'.format(samples // population)

        fig.savefig(fn, bbox_inches='tight', pad_inches=0, dpi=150)


# def correlation() -> None:
#     df_1 = pd.read_csv('bnas_1_random.csv')[:50]
#     df_2 = pd.read_csv('bnas_2_random.csv')[:50]
#     df_1.columns = ['config', 'date', 'params', 'latency', 'macs', 'accuracy_top1']
#     df_2.columns = ['config', 'date', 'params', 'latency', 'macs', 'accuracy_top1']

#     plot_correlation(x1=df_1['accuracy_top1'], x2=df_2['accuracy_top1'])


if __name__ == '__main__':
    if False:
        # warm-up test
        plot_search_progression(
            title='BERT Wamrup',
            results_path='tmp.csv',
            target_metrics=['macs', 'accuracy_sst2'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'accuracy_sst2'],
        )
        plot_search_progression(
            title='BERT Full',
            results_path='tmp_completed.csv',
            target_metrics=['macs', 'accuracy_sst2'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'accuracy_sst2'],
        )
        exit()
    # plot_search_progression(
    #     # results_path='bnas_1_latency.csv',
    #     # results_path='results_tlt_linas_dist2_long.csv',
    #     # results_path='results_ofambv3_random_long.csv',
    #     results_path='bootstrapnas_resnet50_cifar10_linas.csv',
    #     # random_results_path='test.csv',
    #     # random_results_path='bnas_2_random.csv',
    #     # random_results_path='bnas_mbv2_cifar_top1_macs_random.csv',
    # )

    # plot_search_progression(
    #     results_path='bootstrapnas_resnet50_cifar10_random.csv',
    # )
    #  plot_search_progression(results_path='test_nsga.csv')
    #  plot_search_progression(results_path='test_nsga2.csv')
    #  plot_search_progression(results_path='test_random.csv')
    #  plot_search_progression(results_path='test.csv')
    if False:
        plot_search_progression(results_path='bert_sst2_linas.csv')  # , random_results_path='bert_sst2_random.csv')
        plot_search_progression(results_path='bert_sst2_nsga2.csv')  # , random_results_path='bert_sst2_random.csv')
        plot_search_progression(results_path='bert_sst2_random.csv')
    # plot_hv()
    if False:
        plot_search_progression(
            results_path='/nfs/site/home/mszankin/store/nosnap/results/dynast/dynast_vit_linas_a100.csv'
        )
    if False:
        plot_search_progression(
            results_path='/localdisk/maciej/code/dynast-decoma/tmp.csv',
            target_metrics=['cycles', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'accuracy_top1', 'cycles'],
        )
    if False:
        evals_limit = 1000
        # Model's latency: 78.156 +/- 1.453 accuracy_sst2 0.9208715596330275
        plot_search_progression(
            results_path='results_linas_subnet_qp_icx.csv',
            target_metrics=['latency', 'accuracy_sst2'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_sst2'],
            evals_limit=evals_limit,
            reference_points=[
                ReferencePoint(
                    'INC INT8 BERT-SST2',
                    {'latency': 78.156, 'accuracy_sst2': 0.9208715596330275},
                    color='tab:red',
                ),
                ReferencePoint(
                    'FP32 BERT-SST2',
                    {'latency': 170.272, 'accuracy_sst2': 0.9243119266055045},
                    color='tab:purple',
                ),
            ],
        )
    if False:
        evals_limit = 1000
        # Model's latency: 78.156 +/- 1.453 accuracy_sst2 0.9208715596330275
        plot_search_progression(
            results_path='bert_bs1_seq16.csv',
            target_metrics=['latency', 'accuracy_sst2'],
            columns=['config', 'date', 'latency', 'model_size', 'accuracy_sst2'],
            evals_limit=evals_limit,
            reference_points=[
                ReferencePoint(
                    'subnet',
                    {'latency': 8.864, 'accuracy_sst2': 0.925459},
                    color='tab:cyan',
                ),
                ReferencePoint(
                    'FP32 SuperNet',
                    {'latency': 14.342, 'accuracy_sst2': 0.9243119266055045},
                    color='tab:red',
                ),
                ReferencePoint(
                    'INC INT8 SuperNet',
                    {'latency': 11.8, 'accuracy_sst2': 0.9208715596330275},
                    color='tab:orange',
                ),
                ReferencePoint(
                    'FP32 HuggingFace',
                    {'latency': 14.274, 'accuracy_sst2': 0.9174311926605505},
                    color='tab:purple',
                ),
                ReferencePoint(
                    'INC INT8 HuggingFace',
                    {'latency': 11.843, 'accuracy_sst2': 0.9139908256880734},
                    color='tab:pink',
                ),
            ],
        )
        # plot_search_progression(
        #     results_path='results_linas_subnet_qp_spr.csv',
        #     target_metrics=['latency', 'accuracy_sst2'],
        #     columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_sst2'],
        #     evals_limit=evals_limit,
        # )
        # plot_search_progression(
        #     results_path='results_linas_subnet_qp_spr_ompreduced.csv',
        #     target_metrics=['latency', 'accuracy_sst2'],
        #     columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_sst2'],
        #     evals_limit=evals_limit,
        # )
        # plot_search_progression(
        #     results_path='results_linas_subnet_qp_spr_ompreduced_boost.csv',
        #     target_metrics=['latency', 'accuracy_sst2'],
        #     columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_sst2'],
        #     evals_limit=evals_limit,
        # )
        # plot_search_progression(
        #     results_path='results_linas_subnet_qp_spr_ompreduced_noboost.csv',
        #     target_metrics=['latency', 'accuracy_sst2'],
        #     columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_sst2'],
        #     evals_limit=evals_limit,
        # )
    if False:
        import pandas as pd

        df = pd.concat(
            [
                pd.read_csv('qvit_results/qvit_random_spr_tf02_s31_incfix.csv'),
                pd.read_csv('qvit_results/qvit_random_spr_tf02_s32_incfix.csv'),
            ]
        )
        print(len(df))
        df.to_csv('qvit_results/qvit_random_spr_tf02_combined_incfix.csv', index=False)

        for f in [
            'qvit_results/qvit_nsga2_spr_tf02_s20_incfix.csv',
            'qvit_results/qvit_nsga2_spr_tf10_s20_incfix.csv',
            'qvit_results/qvit_linas_spr_tf02_s20_incfix.csv',
            'qvit_results/qvit_linas_spr_tf10_s20_incfix.csv',
        ]:
            plot_search_progression(
                results_path=f,
                target_metrics=['model_size', 'accuracy_top1'],
                columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
                # random_results_path='qvit_results/qvit_random_spr_tf02_combined_incfix.csv',
                reference_points=[
                    #    ReferencePoint(
                    #        'SuperNet FP32',
                    #        {'model_size': 346.516, 'accuracy_top1': 79.497},
                    #        color='tab:orange',
                    #    ),
                    #    ReferencePoint(
                    #        'SuperNet INT8',
                    #        {'model_size': 89.111, 'accuracy_top1': 69.280},
                    #        color='tab:red',
                    #    ),
                ],
                # evals_limit=evals_limit,
            )
    ## QUANT PAPER
    if False:
        plot_search_progression(
            title='OFA ResNet50\nLatency vs. Accuracy',
            results_path='results/ofa_resnet50/dynast_ofaresnet50_quant_linas_sprh9480.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
            reference_points=[
                ReferencePoint(
                    'INT8 ResNet50',
                    {'latency': 69.805, 'accuracy_top1': 75.921},
                    color='tab:orange',
                ),
                ReferencePoint(
                    'INT8 ResNet101',
                    {'latency': 141.542, 'accuracy_top1': 77.283},
                    color='tab:red',
                ),
                ReferencePoint(
                    'INT8 ResNet152',
                    {'latency': 210.97, 'accuracy_top1': 78.233},
                    color='tab:brown',
                ),
            ],
        )

        plot_search_progression(
            title='OFA ResNet50\nModel Size vs. Accuracy',
            results_path='results/ofa/ofaresnet50_linas_tf10_model_size.csv',
            target_metrics=['model_size', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
        )
    if False:
        # BERT
        # plot_search_progression(
        #     results_path='results/qbert/qbert_lians_tf10_latency_fp32_icx.csv',
        #     target_metrics=['latency', 'accuracy_sst2'],
        #     columns=['config', 'date', 'params', 'latency', 'macs', 'accuracy_sst2'],
        # )

        rps = [
            ReferencePoint(
                'INT8 BERT-SST2',
                metrics={
                    'latency': 83.758,
                    'accuracy_sst2': 0.9128440366972477,
                    'model_size': 111.715027,
                },
                color='tab:red',
            ),
        ]
        plot_search_progression(
            title='BERT-SST2\nLatency vs. Accuracy',
            results_path='results/qbert/qbert_lians_tf10_latency_icx.csv',
            target_metrics=['latency', 'accuracy_sst2'],
            columns=['config', 'date', 'latency', 'model_size', 'accuracy_sst2'],
            reference_points=rps,
            normalize=True,
        )
        plot_search_progression(
            title='BERT-SST2\nModel Size vs. Accuracy',
            results_path='results/qbert/qbert_linas_model_size.csv',
            target_metrics=['model_size', 'accuracy_sst2'],
            columns=['config', 'date', 'latency', 'model_size', 'accuracy_sst2'],
            reference_points=rps,
        )
    if False:
        # BEIT
        plot_search_progression(
            title='BEiT\nModel Size vs. Accuracy',
            results_path='results/qbeit/qbeit_linas_tf10.csv',
            target_metrics=['model_size', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'model_size', 'accuracy_top1'],
        )
        plot_search_progression(
            title='BEiT\nLatency vs. Accuracy',
            results_path='results/qbeit/qbeit_linas_tf10_latency_icx.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'model_size', 'accuracy_top1'],
            normalize=True,
        )
    if False:
        # ViT (Endeavour)
        plot_search_progression(
            title='ViT (pretrained)\nLatency vs. Accuracy',
            results_path='results/qvit/qvit_linas_icx_tf10_s20_bs16_latency_icx.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
        )
        plot_search_progression(
            title='ViT (pretrained)\nModel Size vs. Accuracy',
            results_path='results/qvit/qvit_linas_spr_tf10_s20_incfix.csv',
            target_metrics=['model_size', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
        )

        # ViT (k8s)
        # results/k8s-quant/qvit_linas_latency_skx.csv  # single bare
        # results/k8s-quant/qvit_linas_latency_clx_k8s_tf02_single.csv # single k8s
        # results/k8s-quant/qvit_linas_latency_clx.csv  # dist combined bare
        # results/k8s-quant/qvit_linas_latency_clx_k8s_tf02.csv  # dist combined k8s
        plot_search_progression(
            title='ViT\nLatency vs. Accuracy (1xSKX)',
            results_path='results/k8s-quant/qvit_linas_latency_skx.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
        )
        plot_search_progression(
            title='ViT\nLatency vs. Accuracy (1xCLX K8S TF02)',
            results_path='results/k8s-quant/qvit_linas_latency_clx_k8s_tf02_single.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
        )

        plot_search_progression(
            title='ViT\nLatency vs. Accuracy (2xCLX)',
            results_path='results/k8s-quant/qvit_linas_latency_clx.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
        )
        plot_search_progression(
            title='ViT\nLatency vs. Accuracy (2xCLX k8s TF02)',
            results_path='results/k8s-quant/qvit_linas_latency_clx_k8s_tf02.csv',
            target_metrics=['latency', 'accuracy_top1'],
            columns=['config', 'date', 'params', 'latency', 'model_size', 'accuracy_top1'],
            normalize=True,
        )

    if True:
        plot_search_progression(
            title='BERT\nLatency & MACs as proxy',
            results_path='/tmp/results_bert_base_sst2_linas_long_dist.csv',
            target_metrics=['macs', 'accuracy_sst2'],
            columns=['config', 'date', 'params', 'latency', 'macs', 'accuracy_sst2'],
            # xlim=[50, 150],
        )

# correlation()
# results/qbert/qbert_lians_tf10_latency_icx.png results/qbert/qbert_linas_model_size.png results/qbeit/qbeit_linas_tf10.png results/qbeit/qbeit_linas_tf10_latency_icx.png results/qvit/qvit_linas_icx_tf10_s20_bs16_latency_icx.png results/qvit/qvit_linas_spr_tf10_s20_incfix.png dynast_ofaresnet50_quant_linas_sprh9480.png results/ofa/ofaresnet50_linas_tf10_model_size.png


# ofa/ofaresnet50_linas_tf10_model_size.csv
# qbeit/qbeit_linas_tf10_latency_icx.csv
# qbert/qbert_linas_model_size.csv
# qvit/qvit_linas_icx_tf10_s20_bs16_latency_icx.csv

# [11-18 13:59:49] INFO  visualize.py:187 - Loaded 1000 entries from results/ofa_resnet50/dynast_ofaresnet50_quant_linas_sprh9480.csv
# [11-18 13:59:49] INFO  visualize.py:294 - Search progression plot saved to results/ofa_resnet50/dynast_ofaresnet50_quant_linas_sprh9480.png
# [11-18 13:59:49] INFO  visualize.py:187 - Loaded 59 entries from results/ofa/ofaresnet50_linas_tf10_model_size.csv
# [11-18 13:59:50] INFO  visualize.py:294 - Search progression plot saved to results/ofa/ofaresnet50_linas_tf10_model_size.png
# [11-18 13:59:50] INFO  visualize.py:187 - Loaded 1000 entries from results/qbert/qbert_lians_tf10_latency_icx.csv
# [11-18 13:59:50] INFO  visualize.py:294 - Search progression plot saved to results/qbert/qbert_lians_tf10_latency_icx.png
# [11-18 13:59:50] INFO  visualize.py:187 - Loaded 767 entries from results/qbert/qbert_linas_model_size.csv
# [11-18 13:59:51] INFO  visualize.py:294 - Search progression plot saved to results/qbert/qbert_linas_model_size.png
# [11-18 13:59:51] INFO  visualize.py:187 - Loaded 207 entries from results/qbeit/qbeit_linas_tf10.csv
# [11-18 13:59:51] INFO  visualize.py:294 - Search progression plot saved to results/qbeit/qbeit_linas_tf10.png
# [11-18 13:59:51] INFO  visualize.py:187 - Loaded 197 entries from results/qbeit/qbeit_linas_tf10_latency_icx.csv
# [11-18 13:59:52] INFO  visualize.py:294 - Search progression plot saved to results/qbeit/qbeit_linas_tf10_latency_icx.png
# [11-18 13:59:52] INFO  visualize.py:187 - Loaded 247 entries from results/qvit/qvit_linas_icx_tf10_s20_bs16_latency_icx.csv
# [11-18 13:59:52] INFO  visualize.py:294 - Search progression plot saved to results/qvit/qvit_linas_icx_tf10_s20_bs16_latency_icx.png
# [11-18 13:59:52] INFO  visualize.py:187 - Loaded 526 entries from results/qvit/qvit_linas_spr_tf10_s20_incfix.csv
# [11-18 13:59:52] INFO  visualize.py:294 - Search progression plot saved to results/qvit/qvit_linas_spr_tf10_s20_incfix.png
